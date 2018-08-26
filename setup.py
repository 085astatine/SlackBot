#!/usr/bin/env python

from setuptools import setup

setup(
    name='slackbot',
    version='0.0.0',
    author='Astatine',
    author_email='astatine085@gmail.com',
    url='https://github.com/085astatine/slackbot',
    packages=[
        'slackbot',
        'slackbot.action'],
    install_requires=[
        'pyyaml',
        'requests',
        'slackclient'],
    test_suite='test')