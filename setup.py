from setuptools import setup

setup(
    name='WorkerPlugin',
    version='1.1',
    packages=['WorkerPlugin'],
    install_requires=
    [
        'Coronado',
        'tornado'
    ],
    author='Mukul Majmudar',
    author_email='mukul@curecompanion.com',
    description='Worker base plugin for Coronado')
