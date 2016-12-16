from __future__ import (absolute_import, print_function)

import re
import shutil

import boto
import sys
from boto.s3.connection import S3Connection
from fabric.state import env
from yaml import load

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
import os
import time
import glob
import logging
import multiprocessing
from multiprocessing.dummy import Pool

from cassandra_backups import logging_helper
from cassandra_backups.utils import (add_s3_arguments, base_parser,
                                     map_wrap, get_s3_connection_host,
                                     check_lzop, check_pv, compressed_pipe,
                                     decompression_pipe)

DEFAULT_CONCURRENCY = max(multiprocessing.cpu_count() - 1, 1)
BUFFER_SIZE = 64  # Default bufsize is 64M
MBFACTOR = float(1 << 20)
MAX_RETRY_COUNT = 4
SLEEP_TIME = 2
SLEEP_MULTIPLIER = 3
UPLOAD_TIMEOUT = 600
DEFAULT_REDUCED_REDUNDANCY = False

logging_helper.configure(
    format='%(name)-12s %(levelname)-8s %(message)s')

logger = logging_helper.CassandraSnapshotterLogger('cassandra_backups.agent')
boto.set_stream_logger('boto', logging.WARNING)


def put_from_manifest(
        s3_bucket, s3_connection_host, s3_ssenc, s3_base_path,
        aws_access_key_id, aws_secret_access_key, manifest,
        bufsize, reduced_redundancy, rate_limit, quiet,
        concurrency=None, incremental_backups=False):
    """
    Uploads files listed in a manifest to amazon S3
    to support larger than 5GB files multipart upload is used (chunks of 60MB)
    files are uploaded compressed with lzop, the .lzo suffix is appended
    """
    exit_code = 0
    bucket = _get_bucket(
        s3_bucket, aws_access_key_id,
        aws_secret_access_key, s3_connection_host)
    manifest_fp = open(manifest, 'r')
    buffer_size = int(bufsize * MBFACTOR)
    files = manifest_fp.read().splitlines()
    pool = Pool(concurrency)
    for f in pool.imap(_upload_file,
                       ((bucket, f, _destination_path(s3_base_path, f), s3_ssenc, buffer_size, reduced_redundancy,
                         rate_limit, quiet)
                        for f in files if f)):
        if f is None:
            # Upload failed.
            exit_code = 1
        elif incremental_backups:
            # Delete files that were successfully uploaded.
            os.remove(f)
    pool.terminate()
    exit(exit_code)


@map_wrap
def _upload_file(bucket, source, destination, s3_ssenc, bufsize, reduced_redundancy, rate_limit, quiet):
    mp = None
    retry_count = 0
    sleep_time = SLEEP_TIME
    while True:
        try:
            if mp is None:
                # Initiate the multi-part upload.
                try:
                    mp = bucket.initiate_multipart_upload(destination, encrypt_key=s3_ssenc,
                                                          reduced_redundancy=reduced_redundancy)
                    logger.info("Initialized multipart upload for file {!s} to {!s}".format(source, destination))
                except Exception as exc:
                    logger.error(
                        "Error while initializing multipart upload for file {!s} to {!s}".format(source, destination))
                    logger.error(exc.message)
                    raise

            try:
                for i, chunk in enumerate(compressed_pipe(source, bufsize, rate_limit, quiet)):
                    mp.upload_part_from_file(chunk, i + 1, cb=s3_progress_update_callback)
            except Exception as exc:
                logger.error("Error uploading file {!s} to {!s}".format(source, destination))
                logger.error(exc.message)
                raise

            try:
                mp.complete_upload()
            except Exception as exc:
                logger.error("Error completing multipart file upload for file {!s} to {!s}".format(source, destination))
                logger.error(exc.message)
                # The multi-part object may be in a bad state.  Extract an error
                # message if we can, then discard it.
                try:
                    logger.error(mp.to_xml())
                except Exception as exc:
                    pass
                _cancel_upload(bucket, mp, destination)
                mp = None
                raise

            # Successful upload, return the uploaded file.
            return source
        except Exception as exc:
            # Failure anywhere reaches here.
            retry_count += 1
            if retry_count > MAX_RETRY_COUNT:
                logger.error("Retried too many times uploading file {!s}".format(source))
                # Abort the multi-part upload if it was ever initiated.
                if mp is not None:
                    _cancel_upload(bucket, mp, destination)
                return None
            else:
                logger.info("Sleeping before retry")
                time.sleep(sleep_time)
                sleep_time *= SLEEP_MULTIPLIER
                logger.info("Retrying {}/{}".format(retry_count, MAX_RETRY_COUNT))
                # Go round again.


def _get_bucket(
        s3_bucket, aws_access_key_id,
        aws_secret_access_key, s3_connection_host):
    connection = S3Connection(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        host=s3_connection_host
    )
    return connection.get_bucket(s3_bucket, validate=False)


def _destination_path(s3_base_path, file_path, compressed=True):
    suffix = compressed and '.lzo' or ''
    return '/'.join([s3_base_path, file_path + suffix])


def _cancel_upload(bucket, mp, remote_path):
    """
    Safe way to cancel a multipart upload
    sleeps SLEEP_TIME seconds and then makes sure that there are not parts left
    in storage
    """
    attempts = 0
    while attempts < 5:
        try:
            time.sleep(SLEEP_TIME)
            mp.cancel_upload()
            time.sleep(SLEEP_TIME)
            for mp in bucket.list_multipart_uploads():
                if mp.key_name == remote_path:
                    mp.cancel_upload()
            return
        except Exception:
            logger.error("Error while cancelling multipart upload")
            attempts += 1


def create_upload_manifest(
        snapshot_name, snapshot_keyspaces, snapshot_table,
        conf_path, manifest_path, exclude_tables, incremental_backups=False):
    if snapshot_keyspaces:
        keyspace_globs = snapshot_keyspaces.split(',')
    else:
        keyspace_globs = ['*']

    if snapshot_table:
        table_glob = snapshot_table
    else:
        table_glob = '*'

    data_paths = _get_data_path(conf_path)
    files = []
    exclude_tables_list = exclude_tables.split(',')
    for data_path in data_paths:
        for keyspace_glob in keyspace_globs:
            path = [
                data_path,
                keyspace_glob,
                table_glob
            ]
            if incremental_backups:
                path += ['backups']
            else:
                path += ['snapshots', snapshot_name]
            path += ['*']

            path = os.path.join(*path)
            if len(exclude_tables_list) > 0:
                for f in glob.glob(os.path.join(path)):
                    # The current format of a file path looks like:
                    # /var/lib/cassandra/data03/system/compaction_history/snapshots/20151102182658/system-compaction_history-jb-6684-Summary.db
                    if f.split('/')[-4] not in exclude_tables_list:
                        files.append(f.strip())
            else:
                files.append(f.strip() for f in glob.glob(os.path.join(path)))

    with open(manifest_path, 'w') as manifest:
        manifest.write('\n'.join("%s" % f for f in files))


def _get_data_path(conf_path):
    """Retrieve cassandra data_file_directories from cassandra.yaml"""
    config_file_path = os.path.join(conf_path, 'cassandra.yaml')
    with open(config_file_path, 'r') as f:
        cassandra_configs = load(f, Loader=Loader)
    data_paths = cassandra_configs['data_file_directories']
    return data_paths


def local_restore(keyspace, snapshot_path, aws_access_key_id, aws_secret_access_key, host,
                  s3_host, s3_bucket_name, cassandra_tools_bin_dir, restore_dir):
    matcher_string = "(%(host)s).*/(%(keyspace)s)/(%(table)s-[A-Za-z0-9]*)/" % dict(
        host=host, keyspace=keyspace, table=".*?")
    keyspace_path = "/".join([restore_dir, keyspace])
    keyspace_table_matcher = re.compile(matcher_string)

    s3connection = S3Connection(
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        host=s3_host)

    snapshot_keys = [] # baseline
    backups_keys = []  # incremental
    tables = set()

    bucket = s3connection.get_bucket(s3_bucket_name, validate=False)

    for k in bucket.list(snapshot_path):
        logging.info(k)
        # logging.info(k.name)
        r = keyspace_table_matcher.search(k.name)
        if not r:
            continue

        tables.add(r.group(3))
        if 'backups' in k.name:
            backups_keys.append(k)
        if 'snapshots' in k.name:
            snapshot_keys.append(k)

    _delete_old_dir_and_create_new(keyspace_path, tables)

    snapshots_size = reduce(lambda s, k: s + k.size, snapshot_keys, 0)
    backups_size = reduce(lambda s, k: s + k.size, backups_keys, 0)

    logging.info("Found %(files_count)d backup files, with total size \
        of %(size)s." % dict(files_count=len(backups_keys),
                             size=_human_size(backups_size)))
    logging.info("Found %(files_count)d snapshot files, with total size \
        of %(size)s." % dict(files_count=len(snapshot_keys),
                             size=_human_size(snapshots_size)))

    # order relevant: first backups_keys (more recent), then snapshots_keys (don't override files)
    _download_keys(backups_keys, backups_size, restore_dir, keyspace_table_matcher)
    _download_keys(snapshot_keys, snapshots_size, restore_dir, keyspace_table_matcher)

    logging.info("Finished downloading...")

    _run_sstableloader(keyspace_path, tables, host, cassandra_tools_bin_dir)


def _delete_old_dir_and_create_new(keyspace_path, tables):
    if os.path.exists(keyspace_path) and os.path.isdir(keyspace_path):
        logging.warning("Deleting directory ({!s})...".format(
            keyspace_path))
        shutil.rmtree(keyspace_path)

    for table in tables:
        path = "{!s}/{!s}".format(keyspace_path, table)
        if not os.path.exists(path):
            os.makedirs(path, mode=0777)


def _download_keys(keys, total_size, restore_dir, keyspace_table_matcher, pool_size=5):
    logging.info("Starting to download...")

    progress_string = ""
    read_bytes = 0

    thread_pool = Pool(pool_size)

    download_key = lambda key: _download_key(restore_dir, keyspace_table_matcher, key)
    for size in thread_pool.imap(download_key, keys):
        old_width = len(progress_string)
        read_bytes += size
        progress_string = "{!s} / {!s} ({:.2f})".format(
            _human_size(read_bytes),
            _human_size(total_size),
            (read_bytes / float(total_size)) * 100.0)
        width = len(progress_string)
        padding = ""
        if width < old_width:
            padding = " " * (width - old_width)
        progress_string = "{!s}{!s}\r".format(progress_string, padding)

        sys.stderr.write(progress_string)


def _download_key(restore_dir, keyspace_table_matcher, key):
    r = keyspace_table_matcher.search(key.name)
    keyspace = r.group(2)
    table = r.group(3)
    host = key.name.split('/')[2]
    if env.host_string != host:
        logging.info("saved host %s doesn't match current host %s , skipping" % (host, env.host_string))  # TODO check
    file_ = key.name.split('/')[-1]
    filename = "{}/{}/{}".format(keyspace, table, file_)
    key_full_path = os.path.join(restore_dir, filename)

    if filename.endswith('.lzo'):
        uncompressed_key_full_path = re.sub('\.lzo$', '', key_full_path)
        logging.info("Decompressing %s..." % key_full_path)
        lzop_pipe = decompression_pipe(uncompressed_key_full_path)
        try:
            key.open_read()
            for chunk in key:
                lzop_pipe.stdin.write(chunk)
            key.close()
            out, err = lzop_pipe.communicate()
            errcode = lzop_pipe.returncode
            if errcode != 0:
                logging.exception("lzop Out: %s\nError:%s\nExit Code %d: " % (out, err, errcode))
        except Exception as e:
            logging.error('LZOP crashed decompressing "{!s}": {!s} - Could be that the file exists already'.format(
                key_full_path, e))
    else:
        try:
            logging.info("Saving %s..." % key_full_path)
            key.get_contents_to_filename(key_full_path)
        except Exception as e:
            logging.error('Unable to create "{!s}": {!s}'.format(key_full_path, e))

    return key.size


def _human_size(size):
    for x in ['bytes', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return "{:3.1f}{!s}".format(size, x)
        size /= 1024.0
    return "{:3.1f}{!s}".format(size, 'TB')


def _run_sstableloader(keyspace_path, tables, host, cassandra_tools_bin_dir):
    sstableloader = "{!s}/sstableloader".format(cassandra_tools_bin_dir)
    for table in tables:
        path = "/".join([keyspace_path, table])
        if not os.path.exists(path):
            os.makedirs(path, mode=0777)
        command = '%(sstableloader)s --nodes %(hosts)s -v \
            %(keyspace_path)s/%(table)s' % dict(sstableloader=sstableloader, hosts=host,  # FIXME ','.join(hosts),
                                                keyspace_path=keyspace_path, table=table)
        logging.info("invoking: {!s}".format(command))
        os.system(command)


def s3_progress_update_callback(*args):
    # required
    pass


def main():
    subparsers = base_parser.add_subparsers(
        title='subcommands', dest='subcommand')
    base_parser.add_argument(
        '--incremental_backups', action='store_true', default=False)

    put_parser = subparsers.add_parser(
        'put', help="put files on s3 from a manifest")
    manifest_parser = subparsers.add_parser(
        'create-upload-manifest', help="put files on s3 from a manifest")
    fetch_parser = subparsers.add_parser(
        'fetch', help="fetch files from s3")

    # put arguments
    put_parser = add_s3_arguments(put_parser)
    put_parser.add_argument('--bufsize', required=False, type=int, default=BUFFER_SIZE,
                            help="Compress and upload buffer size")
    put_parser.add_argument('--manifest', required=True,
                            help="The manifest containing the files to put on s3")
    put_parser.add_argument('--concurrency', required=False, type=int, default=DEFAULT_CONCURRENCY,
                            help="Compress and upload concurrent processes")
    put_parser.add_argument('--reduced-redundancy', required=False, default=DEFAULT_REDUCED_REDUNDANCY,
                            help="Compress and upload concurrent processes", action="store_true")
    put_parser.add_argument('--rate-limit', required=False, type=int, default=0,
                            help="Limit the upload speed to S3 (by using 'pv'). Value expressed in kilobytes (*1024)")
    put_parser.add_argument('--quiet',
                            help="pv quiet mode, useful when called by a script.", action='store_true')

    # create-upload-manifest arguments
    manifest_parser.add_argument('--snapshot_name', required=True, type=str)
    manifest_parser.add_argument('--conf_path', required=True, type=str)
    manifest_parser.add_argument('--manifest_path', required=True, type=str)
    manifest_parser.add_argument('--snapshot_keyspaces', required=False, type=str, default='')
    manifest_parser.add_argument('--snapshot_table', required=False, type=str, default='')
    manifest_parser.add_argument('--exclude_tables', required=False, type=str)

    # fetch arguments
    fetch_parser.add_argument('--keyspace', required=True, type=str)
    fetch_parser.add_argument('--snapshot-path', required=True, type=str)
    fetch_parser.add_argument('--aws-access-key-id', required=True, type=str)
    fetch_parser.add_argument('--aws-secret-access-key', required=True, type=str)
    fetch_parser.add_argument('--s3-host', required=True, type=str)
    fetch_parser.add_argument('--s3-bucket-name', required=True, type=str)
    fetch_parser.add_argument('--host', required=True, type=str)
    fetch_parser.add_argument('--cassandra-tools-bin-dir', required=True, type=str)
    fetch_parser.add_argument('--restore-dir', required=True, type=str)

    args = base_parser.parse_args()
    subcommand = args.subcommand

    if subcommand == 'create-upload-manifest':
        create_upload_manifest(
            args.snapshot_name,
            args.snapshot_keyspaces,
            args.snapshot_table,
            args.conf_path,
            args.manifest_path,
            args.exclude_tables,
            args.incremental_backups
        )

    if subcommand == 'put':
        check_lzop()

        if args.rate_limit > 0:
            check_pv()

        put_from_manifest(
            args.s3_bucket_name,
            get_s3_connection_host(args.s3_bucket_region),
            args.s3_ssenc,
            args.s3_base_path,
            args.aws_access_key_id,
            args.aws_secret_access_key,
            args.manifest,
            args.bufsize,
            args.reduced_redundancy,
            args.rate_limit,
            args.quiet,
            args.concurrency,
            args.incremental_backups
        )

    if subcommand == 'fetch':
        #check_lzop()
        local_restore(args.keyspace,
                      args.snapshot_path,
                      args.aws_access_key_id,
                      args.aws_secret_access_key,
                      args.host,
                      args.s3_host,
                      args.s3_bucket_name,
                      args.cassandra_tools_bin_dir,
                      args.restore_dir)

if __name__ == '__main__':
    main()
