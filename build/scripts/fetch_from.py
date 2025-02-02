import urllib2
import hashlib
import tarfile
import random
import string
import sys
import os
import logging
import json
import socket
import shutil
import errno
import datetime as dt

import retry

INFRASTRUCTURE_ERROR = 12


def make_user_agent():
    return 'fetch_from: {host}'.format(host=socket.gethostname())


def add_common_arguments(parser):
    parser.add_argument('--copy-to')  # used by jbuild in fetch_resource
    parser.add_argument('--rename-to')  # used by test_node in inject_mds_resource_to_graph
    parser.add_argument('--copy-to-dir')
    parser.add_argument('--untar-to')
    parser.add_argument('--rename', action='append', default=[], metavar='FILE', help='rename FILE to the corresponding output')
    parser.add_argument('--executable', action='store_true', help='make outputs executable')
    parser.add_argument('--log-path')
    parser.add_argument('outputs', nargs='*')


def ensure_dir(path):
    if not (path == '' or os.path.isdir(path)):
        os.makedirs(path)


def hardlink_or_copy(src, dst):
    ensure_dir(os.path.dirname(dst))

    if os.name == 'nt':
        shutil.copy(src, dst)
    else:
        try:
            os.link(src, dst)
        except OSError as e:
            if e.errno == errno.EEXIST:
                return
            elif e.errno == errno.EXDEV:
                sys.stderr.write("Can't make cross-device hardlink - fallback to copy: {} -> {}\n".format(src, dst))
                shutil.copy(src, dst)
            else:
                raise


def rename_or_copy_and_remove(src, dst):
    ensure_dir(os.path.dirname(dst))

    try:
        os.rename(src, dst)
    except OSError:
        shutil.copy(src, dst)
        os.remove(src)


class BadChecksumFetchError(Exception):
    pass


class IncompleteFetchError(Exception):
    pass


class ResourceUnpackingError(Exception):
    pass


class ResourceIsDirectoryError(Exception):
    pass


class OutputIsDirectoryError(Exception):
    pass


class OutputNotExistError(Exception):
    pass


def setup_logging(args, base_name):
    def makedirs(path):
        try:
            os.makedirs(path)
        except OSError:
            pass

    if args.log_path:
        log_file_name = args.log_path
    else:
        log_file_name = base_name + ".log"

    args.abs_log_path = os.path.abspath(log_file_name)
    makedirs(os.path.dirname(args.abs_log_path))
    logging.basicConfig(filename=args.abs_log_path, level=logging.DEBUG)


def is_temporary(e):
    return not isinstance(e, ResourceUnpackingError)


def uniq_string_generator(size=6, chars=string.ascii_lowercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


def report_to_snowden(value):
    def inner():
        body = {
            'namespace': 'ygg',
            'key': 'fetch-from-sandbox',
            'value': json.dumps(value),
        }

        urllib2.urlopen(
            'https://back-snowden.qloud.yandex-team.ru/report/add',
            json.dumps([body, ]),
            timeout=5,
        )

    try:
        inner()
    except Exception as e:
        logging.error(e)


def copy_stream(read, *writers, **kwargs):
    chunk_size = kwargs.get('size', 1024*1024)
    while True:
        data = read(chunk_size)
        if not data:
            break
        for write in writers:
            write(data)


def md5file(fname):
    res = hashlib.md5()
    with open(fname, 'rb') as f:
        copy_stream(f.read, res.update)
    return res.hexdigest()


def git_like_hash_with_size(filepath):
    """
    Calculate git like hash for path
    """
    sha = hashlib.sha1()

    file_size = 0

    with open(filepath, 'rb') as f:
        while True:
            block = f.read(2 ** 16)

            if not block:
                break

            file_size += len(block)
            sha.update(block)

    sha.update('\0')
    sha.update(str(file_size))

    return sha.hexdigest(), file_size


def size_printer(display_name, size):
    sz = [0]
    last_stamp = [dt.datetime.now()]

    def printer(chunk):
        sz[0] += len(chunk)
        now = dt.datetime.now()
        if last_stamp[0] + dt.timedelta(seconds=10) < now:
            if size:
                print >>sys.stderr, "##status##{} - [[imp]]{:.1f}%[[rst]]".format(display_name, 100.0 * sz[0] / size)
            last_stamp[0] = now

    return printer


def fetch_url(url, unpack, resource_file_name, expected_md5=None, expected_sha1=None, tries=10):
    logging.info('Downloading from url %s name %s and expected md5 %s', url, resource_file_name, expected_md5)
    tmp_file_name = uniq_string_generator()

    request = urllib2.Request(url, headers={'User-Agent': make_user_agent()})
    req = retry.retry_func(lambda: urllib2.urlopen(request, timeout=30), tries=tries, delay=5, backoff=1.57079)
    logging.debug('Headers: %s', req.headers.headers)
    expected_file_size = int(req.headers['Content-Length'])
    real_md5 = hashlib.md5()
    real_sha1 = hashlib.sha1()

    with open(tmp_file_name, 'wb') as fp:
        copy_stream(req.read, fp.write, real_md5.update, real_sha1.update, size_printer(resource_file_name, expected_file_size))

    real_md5 = real_md5.hexdigest()
    real_file_size = os.path.getsize(tmp_file_name)
    real_sha1.update('\0')
    real_sha1.update(str(real_file_size))
    real_sha1 = real_sha1.hexdigest()

    if unpack:
        tmp_dir = tmp_file_name + '.dir'
        os.makedirs(tmp_dir)
        with tarfile.open(tmp_file_name, mode="r|gz") as tar:
            tar.extractall(tmp_dir)
        tmp_file_name = os.path.join(tmp_dir, resource_file_name)
        real_md5 = md5file(tmp_file_name)

    logging.info('File size %s (expected %s)', real_file_size, expected_file_size)
    logging.info('File md5 %s (expected %s)', real_md5, expected_md5)
    logging.info('File sha1 %s (expected %s)', real_sha1, expected_sha1)

    if expected_md5 and real_md5 != expected_md5:
        report_to_snowden(
            {
                'headers': req.headers.headers,
                'expected_md5': expected_md5,
                'real_md5': real_md5
            }
        )

        raise BadChecksumFetchError(
            'Downloaded {}, but expected {} for {}'.format(
                real_md5,
                expected_md5,
                url,
            )
        )

    if expected_sha1 and real_sha1 != expected_sha1:
        report_to_snowden(
            {
                'headers': req.headers.headers,
                'expected_sha1': expected_sha1,
                'real_sha1': real_sha1
            }
        )

        raise BadChecksumFetchError(
            'Downloaded {}, but expected {} for {}'.format(
                real_sha1,
                expected_sha1,
                url,
            )
        )

    if expected_file_size != real_file_size:
        report_to_snowden({'headers': req.headers.headers, 'file_size': real_file_size})

        raise IncompleteFetchError(
            'Downloaded {}, but expected {} for {}'.format(
                real_file_size,
                expected_file_size,
                url,
            )
        )

    return tmp_file_name


def process(fetched_file, file_name, args, remove=True):
    assert len(args.rename) <= len(args.outputs), (
        'too few outputs to rename', args.rename, 'into', args.outputs)

    if not os.path.isfile(fetched_file):
        raise ResourceIsDirectoryError('Resource must be a file, not a directory: %s' % fetched_file)

    if args.copy_to:
        hardlink_or_copy(fetched_file, args.copy_to)
        if not args.outputs:
            args.outputs = [args.copy_to]

    if args.rename_to:
        args.rename.append(fetched_file)
        if not args.outputs:
            args.outputs = [args.rename_to]

    if args.copy_to_dir:
        hardlink_or_copy(fetched_file, os.path.join(args.copy_to_dir, file_name))

    if args.untar_to:
        ensure_dir(args.untar_to)
        try:
            with tarfile.open(fetched_file, mode='r:*') as tar:
                tar.extractall(args.untar_to)
        except tarfile.ReadError as e:
            logging.exception(e)
            raise ResourceUnpackingError('File {} cannot be untared'.format(fetched_file))

    for src, dst in zip(args.rename, args.outputs):
        if src == 'RESOURCE':
            src = fetched_file
        if os.path.abspath(src) == os.path.abspath(fetched_file):
            logging.info('Copying %s to %s', src, dst)
            hardlink_or_copy(src, dst)
        else:
            logging.info('Renaming %s to %s', src, dst)
            if remove:
                rename_or_copy_and_remove(src, dst)
            else:
                shutil.copy(src, dst)

    for path in args.outputs:
        if not os.path.exists(path):
            raise OutputNotExistError('Output does not exist: %s' % os.path.abspath(path))
        if not os.path.isfile(path):
            raise OutputIsDirectoryError('Output must be a file, not a directory: %s' % os.path.abspath(path))
        if args.executable:
            os.chmod(path, os.stat(path).st_mode | 0o111)
        if os.path.abspath(path) == os.path.abspath(fetched_file):
            remove = False

    if remove:
        os.remove(fetched_file)
