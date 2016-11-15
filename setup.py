#!/usr/bin/env python

from setuptools import setup, find_packages
from cassandra_backups import __version__


install_requires = [
    'argparse',
    'fabric',
    'boto>=2.29.1',
    'pyyaml'
]

setup(
    name='cassandra_backups',
    version=__version__,
    maintainer='Juan Alvarez',
    maintainer_email='juan@opbeat.com',
    url='http://github.com/jalvz/cassandra_backups',
    description='cassandra_backups is a tool to backup a Cassandra cluster to Amazon S3.',
    packages=find_packages(),
    zip_safe=False,
    install_requires=install_requires,
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'cassandra-backups = cassandra_backups.main:main',
            'cassandra-backups-agent = cassandra_backups.agent:main'
        ]
    },
    classifiers=[
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'Operating System :: OS Independent',
        'Topic :: Software Development',
        'License :: OSI Approved :: GNU General Public License (GPL)',
        'Natural Language :: English',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ],
    keywords=['cassandra', 'AWS', 'S3', 'backup']
)
