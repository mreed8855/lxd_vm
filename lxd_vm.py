#!/usr/bin/env python3
"""
    Test kvm using lxd
"""

from argparse import ArgumentParser
import os
import logging
import lsb_release
import requests
import shlex
from subprocess import (
    Popen,
    PIPE,
    DEVNULL,
    CalledProcessError,
    check_output,
    call
)
import sys
import tempfile
import tarfile
import time
import apt
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4

DEFAULT_TIMEOUT = 500


class RunCommand(object):
    """
    Runs a command and can return all needed info:
    * stdout
    * stderr
    * return code
    * original command
    Convenince class to avoid the same repetitive code to run shell
    commands.
    """

    def __init__(self, cmd=None):
        self.stdout = None
        self.stderr = None
        self.returncode = None
        self.cmd = cmd
        self.run(self.cmd)

    def run(self, cmd):
        proc = Popen(shlex.split(cmd), stdout=PIPE, stderr=PIPE,
                     stdin=DEVNULL, universal_newlines=True)
        self.stdout, self.stderr = proc.communicate()
        self.returncode = proc.returncode

    def check_package(self, pkg_name):
        cache = apt.Cache()

        pkg = cache[pkg_name]
        if pkg.is_installed:
            print("{} already installed".format(pkg_name))
        else:
            pkg.mark_install()

        try:
            cache.commit()
        except Exception:
            print("Install of {} failed".format(sys.stderr))


class LXDTest(object):

    def __init__(self, template=None, rootfs=None):
        self.rootfs_url = rootfs
        self.template_url = template
        self.rootfs_tarball = None
        self.template_tarball = None
        self.name = 'testbed'
        self.image_alias = uuid4().hex
        self.default_remote = "ubuntu:"
        self.os_version = get_release_to_test()

    def run_command(self, cmd):
        task = RunCommand(cmd)
        if task.returncode != 0:
            logging.error('Command {} returnd a code of {}'.format(
                task.cmd, task.returncode))
            logging.error(' STDOUT: {}'.format(task.stdout))
            logging.error(' STDERR: {}'.format(task.stderr))
            return False
        else:
            logging.debug('Command {}:'.format(task.cmd))
            if task.stdout != '':
                logging.debug(' STDOUT: {}'.format(task.stdout))
            elif task.stderr != '':
                logging.debug(' STDERR: {}'.format(task.stderr))
            else:
                logging.debug(' Command returned no output')
            return True

    def setup(self):
        # Initialize LXD
        result = True
        logging.debug("Attempting to initialize LXD")
        # TODO: Need a method to see if LXD is already initialized
        if not self.run_command('lxd init --auto'):
            logging.debug('Error encounterd while initializing LXD')
            result = False

        # Retrieve and insert LXD images
        if self.template_url is not None:
            logging.debug("Downloading template.")
            targetfile = urlparse(self.template_url).path.split('/')[-1]
            filename = os.path.join('/tmp', targetfile)
            if not os.path.isfile(filename):
                self.template_tarball = self.download_images(self.template_url,
                                                             filename)
                if not self.template_tarball:
                    logging.error("Unable to download {} from "
                                  "{}".format(self.template_tarball,
                                              self.template_url))
                    logging.error("Aborting")
                    result = False
            else:
                logging.debug("Template file {} already exists. "
                              "Skipping Download.".format(filename))
                self.template_tarball = filename

        if self.rootfs_url is not None:
            logging.debug("Downloading rootfs.")
            targetfile = urlparse(self.rootfs_url).path.split('/')[-1]
            filename = os.path.join('/tmp', targetfile)
            if not os.path.isfile(filename):
                self.rootfs_tarball = self.download_images(self.rootfs_url,
                                                           filename)
                if not self.rootfs_tarball:
                    logging.error("Unable to download {} from{}".format(
                        self.rootfs_tarball, self.rootfs_url))
                    logging.error("Aborting")
                    result = False
            else:
                logging.debug("Template file {} already exists. "
                              "Skipping Download.".format(filename))
                self.rootfs_tarball = filename

        # Insert images
        if self.template_url is not None and self.rootfs_url is not None:
            logging.debug("Importing images into LXD")
            cmd = 'lxc image import {} rootfs {} --alias {}'.format(
                self.template_tarball, self.rootfs_tarball,
                self.image_alias)
            result = self.run_command(cmd)
            if not result:
                logging.error('Error encountered while attempting to '
                              'import images into LXD')
                result = False
        else:
            logging.debug("No local image available, attempting to "
                          "import from default remote.")
            retry = 2
            cmd = 'lxc image copy {}{} local: --alias {}'.format(
                self.default_remote, self.os_version, self.image_alias)
            result = self.run_command(cmd)
            while not result and retry > 0:
                logging.error('Error encountered while attempting to '
                              'import images from default remote.')
                logging.error('Retrying up to {} times.'.format(retry))
                result = self.run_command(cmd)
                retry -= 1
        return result

    def download_images(self, url, filename):
        """
        Downloads LXD files for same release as host machine
        """
        # TODO: Clean this up to use a non-internet simplestream on MAAS server
        logging.debug("Attempting download of {} from {}".format(filename,
                                                                 url))
        try:
            urllib.request.urlretrieve(url, filename)
        except (IOError,
                OSError,
                urllib.error.HTTPError,
                urllib.error.URLError) as exception:
            logging.error("Failed download of image from %s: %s",
                          url, exception)
            return False
        except ValueError as verr:
            logging.error("Invalid URL %s" % url)
            logging.error("%s" % verr)
            return False

        if not os.path.isfile(filename):
            logging.warn("Can not find {}".format(filename))
            return False

        return filename

    def cleanup(self):
        """
        Clean up test files an containers created
        """
        logging.debug('Cleaning up images and containers created during test')
        self.run_command('lxc image delete {}'.format(self.image_alias))
        self.run_command('lxc delete --force {}'.format(self.name))

    def start(self):
        """
        Creates a container and performs the test
        """
        result = self.setup()
        if not result:
            logging.error("One or more setup stages failed.")
            return False

        # Create container
        logging.debug("Launching container")
        if not self.run_command('lxc launch {} {}'.format(self.image_alias,
                                                          self.name)):
            return False

        logging.debug("Container listing:")
        cmd = ("lxc list")
        if not self.run_command(cmd):
            return False

        logging.debug("Testing container")
        cmd = ("lxc exec {} dd if=/dev/urandom of=testdata.txt "
               "bs=1024 count=1000".format(self.name))
        if not self.run_command(cmd):
            return False

        return True

    def start_vm(self):
        """
        Creates an lxd virtutal machine and performs the test
        """
        result = self.setup()
        if not result:
            logging.error("One or more setup stages failed.")
            return False

        # Create container
        logging.debug("Launching container")
        if not self.run_command('lxc launch {} {} -vm'.format(self.image_alias,
                                                              self.name)):
            return False

        logging.debug("Start VM:")
        cmd = ("lxc start {} ".format(self.name))
        if not self.run_command(cmd):
            return False

        logging.debug("Container listing:")
        cmd = ("lxc list")
        if not self.run_command(cmd):
            return False

        logging.debug("Testing container")
        cmd = ("lxc exec {} dd if=/dev/urandom of=testdata.txt "
               "bs=1024 count=1000".format(self.name))
        if not self.run_command(cmd):
            return False

        return True


def test_lxd_vm(args):
    logging.debug("Executing LXD VM Test")

    template = None
    rootfs = None

    # First in priority are environment variables.
    if 'LXD_TEMPLATE' in os.environ:
        template = os.environ['LXD_TEMPLATE']
    if 'LXD_ROOTFS' in os.environ:
        rootfs = os.environ['LXD_ROOTFS']

    # Finally, highest-priority are command line arguments.
    if args.template:
        template = args.template
    if args.rootfs:
        rootfs = args.rootfs

    lxd_test = LXDTest(template, rootfs)

    result = lxd_test.start_vm()
    lxd_test.cleanup()
    if result:
        print("PASS: Container was succssfully started and checked")
        sys.exit(0)
    else:
        print("FAIL: Container was not started and checked")
        sys.exit(1)


def main():
    logging.debug("Executing LXD KM Test in main")

    parser = ArgumentParser(description="Virtualization Test")
    subparsers = parser.add_subparsers()

    # Main cli options
    lxd_test_vm_parser = subparsers.add_parser(
        'lxd', help=("Run the LXD VM validation test"))
    parser.add_argument('--debug', dest='log_level',
                        action="store_const", const=logging.DEBUG,
                        default=logging.INFO)

    # Sub test options
    lxd_test_vm_parser.add_argument(
        '--template', type=str, default=None)
    lxd_test_vm_parser.add_argument(
        '--rootfs', type=str, default=None)
    lxd_test_vm_parser.set_defaults(func=test_lxd_vm)

    args = parser.parse_args()

    try:
        logging.basicConfig(level=args.log_level)
    except AttributeError:
        pass  # avoids exception when trying to run without specifying 'kvm'

    # silence normal output from requests module
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Verify args
    try:
        args.func(args)
    except AttributeError:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
