# -*- coding: utf-8

import datetime
import logging
import pathlib
import queue
import re
from typing import List, NamedTuple, Optional, Pattern
import slack
from .. import Action, Channel, Option, OptionList
from . import download
from ._option import AvatarOption


class DownloadOption(NamedTuple):
    channel: List[str]
    pattern: Pattern
    destination_directory: pathlib.Path
    least_size: Optional[int]
    thread: download.ThreadOption
    avatar: AvatarOption

    @staticmethod
    def option_list(
            name: str,
            help: str = '') -> OptionList['DownloadOption']:
        return OptionList(
            DownloadOption,
            name,
            [Option('channel',
                    action=lambda x: (
                            [] if x is None
                            else [x] if isinstance(x, str)
                            else x),
                    default=None,
                    help='target channel name (list or string)'),
             Option('pattern',
                    action=re.compile,
                    default=r'download\s+"(?P<name>.+)"\s+'
                            r'<(?P<url>https?://[\w/:%#\$&\?\(\)~\.=\+\-]+)'
                            r'(|\|[^>]+)>',
                    help=('regular expresion for working'
                          ' which have simbolic groups named "name" & "url"')),
             Option('destination_directory',
                    action=lambda x: pathlib.Path().joinpath(x),
                    default='./download',
                    help='directory where files are saved'),
             Option('least_size',
                    action=lambda x: int(x) if x is not None else None,
                    help='minimun file size'
                         ' regarded as a successful download'),
             download.ThreadOption.option_list(
                    name='thread',
                    help='download thread'),
             AvatarOption.option_list(
                    name='avatar',
                    help='avatar')],
            help=help)


class ReportInfo(NamedTuple):
    channel: Channel


Report = download.Report[ReportInfo]


class Download(Action[DownloadOption]):
    def __init__(
            self,
            name: str,
            option: DownloadOption,
            logger: Optional[logging.Logger] = None) -> None:
        super().__init__(
                name,
                option,
                logger=logger or logging.getLogger(__name__))
        self._report_queue: 'queue.Queue[Report]' = queue.Queue()
        self._download_threads = download.ThreadGenerator(
                report_queue=self._report_queue,
                option=self.option.thread)

    def register(self) -> None:
        self.register_callback(
                event='message',
                callback=self._callback)

    async def update(self, client: slack.WebClient) -> None:
        while not self._report_queue.empty():
            report = self._report_queue.get()
            self._logger.debug('report: %s', report)
            await _post_report(client, self.option, report)

    def stop(self) -> None:
        self._logger.info('request cancel')
        self._download_threads.cancel()

    @staticmethod
    def option_list(name: str) -> OptionList['DownloadOption']:
        return DownloadOption.option_list(name)

    async def _callback(self, **payload) -> None:
        data = payload['data']
        channel = self.team.channels.id_search(data['channel'])
        if ('subtype' in data
                or channel is None
                or channel.name not in self.option.channel):
            return
        self._logger.debug('match message: %s', data['text'].strip())
        match = self.option.pattern.match(data['text'].strip())
        if not match:
            return
        name = match.group('name')
        url = match.group('url')
        path = self.option.destination_directory.joinpath(name)
        self._logger.info('detect: name=\'%s\', url=\'%s\'', name, url)
        # start thread
        self._download_threads.start(
                url=url,
                path=path,
                info=ReportInfo(channel=channel))


def _start_message(report: Report) -> str:
    return '[{0}]:start <{1}> (size: {2})'.format(
                report.path.name,
                report.final_url,
                download.Report.format_bytes(report.progress.file_size))


def _progress_message(report: Report) -> str:
    message: List[str] = []
    message.append('[{0}]:progress'.format(report.path.name))
    if report.progress.file_size is not None:
        progress_rate = report.progress.progress_rate
        message.append(' {0}/{1} ({2})'.format(
                download.Report.format_bytes(report.progress.downloaded_size),
                download.Report.format_bytes(report.progress.file_size),
                f'{progress_rate:.2%}'
                if progress_rate is not None
                else '--.--%%'))
    else:
        message.append(' {0}'.format(
                download.Report.format_bytes(report.progress.downloaded_size)))
    message.append(' {0}/s in {1}'.format(
            download.Report.format_bytes(report.progress.speed),
            datetime.timedelta(seconds=report.progress.elapsed_time)))
    if report.progress.remaining_time is not None:
        message.append(' (remaining {0})'.format(
                datetime.timedelta(seconds=report.progress.remaining_time)))
    return ''.join(message)


def _finish_report(
        report: Report,
        least_size: Optional[int]) -> str:
    assert report.saved_path is not None
    message: List[str] = []
    if report.path == report.saved_path:
        message.append('[{0}]:finish'.format(report.path.name))
    else:
        message.append('[{0}]->[{1}]:finish'.format(
                report.path.name,
                report.saved_path.name))
    message.append(' {0} at {1}/s in {2}'.format(
            download.Report.format_bytes(report.progress.downloaded_size),
            download.Report.format_bytes(report.progress.average_speed),
            datetime.timedelta(seconds=report.progress.elapsed_time)))
    # file size check
    if (least_size is not None
            and report.progress.downloaded_size < least_size):
        message.append('\n')
        message.append('[{0}]:delete'.format(report.saved_path.name))
        message.append(' because the file is smaller than least size')
        message.append(' ({0} < {1})'.format(
                download.Report.format_bytes(report.progress.downloaded_size),
                download.Report.format_bytes(least_size)))
        # delete
        report.saved_path.unlink()
    return ''.join(message)


def _error_report(report: Report) -> str:
    assert report.error is not None
    return '[{0}]:error {1} {2}'.format(
                report.path.name,
                report.error.__class__.__name__,
                report.error)


async def _post_report(
        client: slack.WebClient,
        option: DownloadOption,
        report: Report) -> None:
    message = ''
    # start
    if report.type is download.ReportType.START:
        message = _start_message(report)
    # progress
    elif report.type is download.ReportType.PROGRESS:
        message = _progress_message(report)
    # finish
    elif report.type is download.ReportType.FINISH:
        message = _finish_report(report, option.least_size)
    # error
    elif report.type is download.ReportType.ERROR:
        message = _error_report(report)
    # post message
    params = {}
    params['text'] = message
    params.update(option.avatar.params())
    await client.chat_postMessage(
            channel=report.info.channel.id,
            **params)
