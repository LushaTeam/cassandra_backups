import os
from collections import defaultdict

from fabric.api import env
import logging

from cassandra_backups.snapshotting import (SnapshotCollection,
                                            BackupWorker,
                                            Snapshot,
                                            RestoreWorker)
from utils import get_s3_connection_host, add_s3_arguments, base_parser

env.use_ssh_config = True


def _set_user_env_vars(args):
    if args.user:
        env.user = args.user
    if args.sudo_user:
        env.sudo_user = args.sudo_user
    if args.password:
        env.password = args.password
    if args.sshkey:
        env.key_filename = args.sshkey
    if args.sshport:
        env.port = args.sshport


def run_backup(args):

    _set_user_env_vars(args)
    env.hosts = args.hosts.split(',')
    env.keyspaces = args.keyspaces.split(',') if args.keyspaces else None

    if args.new_snapshot:
        create_snapshot = True
    else:
        existing_snapshot = SnapshotCollection(
            args.aws_access_key_id,
            args.aws_secret_access_key,
            args.s3_base_path,
            args.s3_bucket_name,
            get_s3_connection_host(args.s3_bucket_region)
        ).get_snapshot_for(
            hosts=env.hosts,
            keyspaces=env.keyspaces,
            table=args.table,
            name=Snapshot.make_snapshot_name()
        )
        create_snapshot = existing_snapshot is None

    worker = BackupWorker(
        aws_access_key_id=args.aws_access_key_id,
        aws_secret_access_key=args.aws_secret_access_key,
        s3_bucket_region=args.s3_bucket_region,
        s3_ssenc=args.s3_ssenc,
        s3_connection_host=get_s3_connection_host(args.s3_bucket_region),
        cassandra_conf_path=args.cassandra_conf_path,
        cassandra_tools_bin_dir=args.cassandra_tools_bin_dir,
        cqlsh_user=args.cqlsh_user,
        cqlsh_password=args.cqlsh_password,
        backup_schema=args.backup_schema,
        buffer_size=args.buffer_size,
        use_sudo=args.use_sudo,
        connection_pool_size=args.connection_pool_size,
        exclude_tables=args.exclude_tables,
        reduced_redundancy=args.reduced_redundancy,
        rate_limit=args.rate_limit,
        quiet=args.quiet
    )
    if create_snapshot:
        logging.info("Make a new snapshot")
        snapshot = Snapshot(
            base_path=args.s3_base_path,
            s3_bucket=args.s3_bucket_name,
            hosts=env.hosts,
            keyspaces=env.keyspaces,
            table=args.table
        )
        worker.snapshot(snapshot)
    else:
        logging.info("Add incrementals to snapshot {!s}".format(
            existing_snapshot))
        worker.update_snapshot(existing_snapshot)


def list_backups(args):
    snapshots = SnapshotCollection(
        args.aws_access_key_id,
        args.aws_secret_access_key,
        args.s3_base_path,
        args.s3_bucket_name,
        get_s3_connection_host(args.s3_bucket_region)
    )
    path_snapshots = defaultdict(list)

    for snapshot in snapshots:
        base_path = os.path.join(snapshot.base_path.split('/')[:-1])
        path_snapshots[base_path].append(snapshot)

    for path, snapshots in path_snapshots.iteritems():
        for snapshot in snapshots:
            print("\t {!r} hosts:{!r} keyspaces:{!r} table:{!r}".format(
                snapshot, snapshot.hosts, snapshot.keyspaces, snapshot.table))
        print("------------------------{}".format('-' * len(path)))


def restore_backup(args):

    _set_user_env_vars(args)
    env.host_string = args.host

    snapshots = SnapshotCollection(
        args.aws_access_key_id,
        args.aws_secret_access_key,
        args.s3_base_path,
        args.s3_bucket_name,
        get_s3_connection_host(args.s3_bucket_region)
    )

    if args.snapshot_name == 'LATEST':
        snapshot = snapshots.get_latest()
    else:
        snapshot = snapshots.get_snapshot_by_name(args.snapshot_name)

    worker = RestoreWorker(aws_access_key_id=args.aws_access_key_id,
                           aws_secret_access_key=args.aws_secret_access_key,
                           s3_bucket_region=args.s3_bucket_region,
                           snapshot=snapshot,
                           cassandra_tools_bin_dir=args.cassandra_tools_bin_dir,
                           restore_dir=args.restore_dir,
                           use_sudo=args.use_sudo)

    worker.restore(args.keyspace)


def main():
    a_base_parser = add_s3_arguments(base_parser)
    subparsers = a_base_parser.add_subparsers(
        title='subcommands', dest='subcommand'
    )

    subparsers.add_parser('list', help="List existing backups")

    backup_parser = subparsers.add_parser('backup', help="Create a snapshot")

    # snapshot / backup arguments
    backup_parser.add_argument(
        '--buffer-size',
        default=64,
        help="The buffer size (MB) for compress and upload")

    backup_parser.add_argument(
        '--exclude-tables',
        default='',
        help="Column families you want to skip")

    backup_parser.add_argument(
        '--hosts',
        required=True,
        help="Comma separated list of hosts to snapshot")

    backup_parser.add_argument(
        '--keyspaces',
        default='',
        help="Comma separated list of keyspaces to backup (omit to backup all)")

    backup_parser.add_argument(
        '--table',
        default='',
        help="The table (column family) to backup")

    backup_parser.add_argument(
        '--cassandra-conf-path',
        default='/etc/cassandra/',
        help="cassandra config file path")

    backup_parser.add_argument(
        '--cassandra-tools-bin-dir',
        default='/usr/bin',
        help="binary directory for nodetool, cqlsh and sstableloader")

    backup_parser.add_argument(
        '--user',
        help="The ssh user to logging on nodes")

    backup_parser.add_argument(
        '--use-sudo',
        default=False,
        help="Use sudo to run backup")

    backup_parser.add_argument(
        '--sudo-user',
        default=None,
        help="Run sudo as an user other than the default (root)")

    backup_parser.add_argument(
        '--sshport',
        help="The ssh port to use to connect to the nodes")

    backup_parser.add_argument(
        '--password',
        default='',
        help="User password to connect with hosts")

    backup_parser.add_argument(
        '--sshkey',
        help="The file containing the private ssh key to use to connect with hosts")

    backup_parser.add_argument(
        '--new-snapshot',
        action='store_true',
        help="Create a new snapshot")

    backup_parser.add_argument(
        '--backup-schema',
        action='store_true',
        help="Backup (thrift) schema of selected keyspaces")

    backup_parser.add_argument(
        '--cqlsh-user',
        default='',
        help="User to use for cqlsh commands")

    backup_parser.add_argument(
        '--cqlsh-password',
        default='',
        help="Password to use for cqlsh commands")

    backup_parser.add_argument(
        '--connection-pool-size',
        default=12,
        help="Number of simultaneous connections to cassandra nodes")

    backup_parser.add_argument(
        '--reduced-redundancy',
        action='store_true',
        help="Use S3 reduced redundancy")

    backup_parser.add_argument(
        '--rate-limit',
        default=0,
        help="Limit the upload speed to S3 (by using 'pv'). Value expressed in kilobytes (*1024)")

    backup_parser.add_argument(
        '--quiet',
        action='store_true',
        help="Set pv in quiet mode when using --rate-limit. "
             "Useful when called by a script.")

    # restore snapshot arguments
    restore_parser = subparsers.add_parser(
        'restore', help="Restores a snapshot")

    restore_parser.add_argument(
        '--snapshot-name',
        default='LATEST',
        help="The name of the snapshot (and incremental backups) to restore")

    restore_parser.add_argument(
        '--keyspace',
        required=True,
        help="The keyspace to restore")

    restore_parser.add_argument(
        '--user',
        help="The ssh user to logging on nodes")

    restore_parser.add_argument(
        '--use-sudo',
        default=False,
        help="Use sudo to restore the backup")

    restore_parser.add_argument(
        '--sudo-user',
        default=None,
        help="Run sudo as an user other than the default (root)")

    restore_parser.add_argument(
        '--sshport',
        help="The ssh port to use to connect to the nodes")

    restore_parser.add_argument(
        '--password',
        default='',
        help="User password to connect with hosts")

    restore_parser.add_argument(
        '--sshkey',
        help="The file containing the private ssh key to use to connect with hosts")

    restore_parser.add_argument(
        '--host',
        required=True,
        help="Cassandra host to restore into")

    restore_parser.add_argument(
        '--cassandra-tools-bin-dir',
        default='/usr/bin',
        help="binary directory for nodetool, cqlsh and sstableloader")

    restore_parser.add_argument(
        '--restore-dir',
        default='/tmp/cassandra_restore',
        help="directory to recover the backup to")

    args = a_base_parser.parse_args()
    subcommand = args.subcommand

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    if subcommand == 'backup':
        run_backup(args)
    elif subcommand == 'list':
        list_backups(args)
    elif subcommand == 'restore':
        restore_backup(args)


if __name__ == '__main__':
    main()
