#! /usr/bin/python3
#
# Copyright (c) 2017 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Common utilities for test cases
"""

import collections
import datetime
import os
import re
import ssl
import time
import traceback
import urllib.parse

import pyte

import commonl
import tcfl.tc

def ansi_render_approx(s, width = 80, height = 2000):
    """
    Does an approximated render of how a string would look on a vt100
    terminal

    The string can contain ANSI escape sequences, which the
    :module:`pyte` engine can render so a string such as

    >>> s = '\x1b[23;08Hi\x1b[23;09H\x1b[23;09Hf\x1b[23;10H\x1b[23;10Hc\x1b[23;11H\x1b[23;11Ho\x1b[23;12H\x1b[23;12Hn\x1b[23;13H\x1b[23;13Hf\x1b[23;14H\x1b[23;14Hi\x1b[23;15H\x1b[23;15Hg\x1b[23;16H\x1b[23;16H \x1b[23;17H\x1b[23;17H-\x1b[23;18H\x1b[23;18Hl\x1b[23;19H\x1b[23;19H \x1b[23;20H\x1b[23;20He\x1b[23;21H\x1b[23;21Ht\x1b[23;22H\x1b[23;22Hh\x1b[23;23H\x1b[23;23H0\x1b[23;24H\x1b[23;24H\r\n\r\n'

    renders to::

      <RENDERER: skipped 23 empty lines>
             ifconfig -l eth0

      Shell> ping -n 5 10.219.169.119

    (loosing attributes such as boldface, colors, etc) Note also that
    the sequences might move the cursor, override things, etc --
    sometimes you need to render different parts of the string and
    feed parts to it and see how it updates it.

    :param str s: string to render, possibly containing ANSI escape
      sequences

    :param int width: (optional) with of the screen where to render
      (usually 80, the original vt100 terminal size)

    :param int height: (optional) height of the screen where to render
      (usually 24, the original vt100 terminal size, however made to
      default to 2000 so it catches history for sequential command
      executions; this might not work on all cases if the sequentials
      clear the screen or move the cursor.
    """
    assert isinstance(s, str)
    assert isinstance(width, int) and width > 20
    assert isinstance(height, int) and height > 20
    r = ""
    screen = pyte.Screen(width, height)
    stream = pyte.Stream(screen)
    stream.feed(s)

    empty_line = width * " "
    last = empty_line
    skips = 1
    for line in screen.display:
        # skip over repeated empty lines
        if line == empty_line and line == last:
            skips += 1
            continue
        else:
            if skips > 1:
                r += f"<RENDERER: skipped {skips} empty lines>\n"
            skips = 1
            last = line
        r += line.rstrip() + "\n"
    return r


def ipxe_sanboot_url(target, sanboot_url):
    """
    Use iPXE to sanboot a given URL

    Given a target than can boot iPXE via a PXE boot entry (normally
    in EFI), drive it to boot iPXE and iPXE to load a sanboot URL so
    any ISO image can be loaded and booted as a local one.

    This is also used in :ref:`the iPXE Sanboot<example_efi_pxe_sanboot>`.

    Requirements: 

    - iPXE: must have Ctrl-B configured to allow breaking into the
      console

    :param tcfl.tc.target_c target: target where to perform the
      operation
    :param str sanboot_url: URL to download and map into a drive to
      boot into. If *skip* nothing is done and you are left with an
      iPXE console connected to the network.
    """
    target.power.cycle()

    boot_ic = target.kws['pos_boot_interconnect']
    mac_addr = target.kws['interconnects'][boot_ic]['mac_addr']
    tcfl.biosl.boot_network_pxe(
        target,
        # Eg: UEFI PXEv4 (MAC:4AB0155F98A1)
        r"UEFI PXEv4 \(MAC:%s\)" % mac_addr.replace(":", "").upper().strip())

    # can't wait also for the "ok" -- debugging info might pop in th emiddle
    target.expect("iPXE initialising devices...")
    # if the connection is slow, we have to start sending Ctrl-B's
    # ASAP
    #target.expect(re.compile("iPXE .* -- Open Source Network Boot Firmware"))

    # send Ctrl-B to go to the PXE shell, to get manual control of iPXE
    #
    # do this as soon as we see the boot message from iPXE because
    # otherwise by the time we see the other message, it might already
    # be trying to boot pre-programmed instructions--we'll see the
    # Ctrl-B message anyway, so we expect for it.
    #
    # before sending these "Ctrl-B" keystrokes in ANSI, but we've seen
    # sometimes the timing window being too tight, so we just blast
    # the escape sequence to the console.
    target.console.write("\x02\x02")	# use this iface so expecter
    time.sleep(0.3)
    target.console.write("\x02\x02")	# use this iface so expecter
    time.sleep(0.3)
    target.console.write("\x02\x02")	# use this iface so expecter
    time.sleep(0.3)
    target.expect("Ctrl-B", timeout = 250)
    target.console.write("\x02\x02")	# use this iface so expecter
    time.sleep(0.3)
    target.console.write("\x02\x02")	# use this iface so expecter
    time.sleep(0.3)
    target.expect("iPXE>")
    prompt_orig = target.shell.shell_prompt_regex
    try:
        #
        # When matching end of line, match against \r, since depends
        # on the console it will send one or two \r (SoL vs SSH-SoL)
        # before \n -- we removed that in the kernel driver by using
        # crnl in the socat config
        #
        # FIXME: block on anything here? consider infra issues
        # on "Connection timed out", http://ipxe.org...
        target.shell.shell_prompt_regex = "iPXE>"
        kws = dict(target.kws)
        boot_ic = target.kws['pos_boot_interconnect']
        mac_addr = target.kws['interconnects'][boot_ic]['mac_addr']
        ipv4_addr = target.kws['interconnects'][boot_ic]['ipv4_addr']
        ipv4_prefix_len = target.kws['interconnects'][boot_ic]['ipv4_prefix_len']
        kws['ipv4_netmask'] = commonl.ipv4_len_to_netmask_ascii(ipv4_prefix_len)

        # Find what network interface our MAC address is; the
        # output of ifstat looks like:
        #
        ## net0: 00:26:55:dd:4a:9d using 82571eb on 0000:6d:00.0 (open)
        ##   [Link:up, TX:8 TXE:1 RX:44218 RXE:44205]
        ##   [TXE: 1 x "Network unreachable (http://ipxe.org/28086090)"]
        ##   [RXE: 43137 x "Operation not supported (http://ipxe.org/3c086083)"]
        ##   [RXE: 341 x "The socket is not connected (http://ipxe.org/380f6093)"]
        ##   [RXE: 18 x "Invalid argument (http://ipxe.org/1c056082)"]
        ##   [RXE: 709 x "Error 0x2a654089 (http://ipxe.org/2a654089)"]
        ## net1: 00:26:55:dd:4a:9c using 82571eb on 0000:6d:00.1 (open)
        ##   [Link:down, TX:0 TXE:0 RX:0 RXE:0]
        ##   [Link status: Down (http://ipxe.org/38086193)]
        ## net2: 00:26:55:dd:4a:9f using 82571eb on 0000:6e:00.0 (open)
        ##   [Link:down, TX:0 TXE:0 RX:0 RXE:0]
        ##   [Link status: Down (http://ipxe.org/38086193)]
        ## net3: 00:26:55:dd:4a:9e using 82571eb on 0000:6e:00.1 (open)
        ##   [Link:down, TX:0 TXE:0 RX:0 RXE:0]
        ##   [Link status: Down (http://ipxe.org/38086193)]
        ## net4: 98:4f:ee:00:05:04 using NII on NII-0000:01:00.0 (open)
        ##   [Link:up, TX:10 TXE:0 RX:8894 RXE:8441]
        ##   [RXE: 8173 x "Operation not supported (http://ipxe.org/3c086083)"]
        ##   [RXE: 268 x "The socket is not connected (http://ipxe.org/380f6093)"]
        #
        # thus we need to match the one that fits our mac address
        ifstat = target.shell.run("ifstat", output = True, trim = True)
        regex = re.compile(
            "(?P<ifname>net[0-9]+): %s using" % mac_addr.lower(),
            re.MULTILINE)
        m = regex.search(ifstat)
        if not m:
            raise tcfl.tc.error_e(
                "iPXE: cannot find interface name for MAC address %s;"
                " is the MAC address in the configuration correct?"
                % mac_addr.lower(),
                dict(target = target, ifstat = ifstat,
                     mac_addr = mac_addr.lower())
            )
        ifname = m.groupdict()['ifname']

        # static is much faster and we know the IP address already
        # anyway; but then we don't have DNS as it is way more
        # complicated to get it
        target.shell.run("set %s/ip %s" % (ifname, ipv4_addr))
        target.shell.run("set %s/netmask %s" % (ifname, kws['ipv4_netmask']))
        target.shell.run("ifopen " + ifname)

        if sanboot_url == "skip":
            target.report_info("not booting", level = 0)
        else:
            target.send("sanboot %s" % sanboot_url)
    finally:
        target.shell.shell_prompt_regex = prompt_orig
    

#! Place where the Zephyr tree is located
# Note we default to empty string so it can be pased
ZEPHYR_BASE = os.environ.get(
    'ZEPHYR_BASE',
    '__environment_variable_ZEPHYR_BASE__not_exported__')

def zephyr_tags():
    """
    Evaluate the build environment and make sure all it is needed to
    build Zephyr apps is in place.

    If not, return a dictionary defining a *skip* tag with the reason
    that can be fed directly to decorator :func:`tcfl.tc.tags`; usage:

    >>> import tcfl.tc
    >>> import qal
    >>>
    >>> @tcfl.tc.tags(**qal.zephyr_tests_tags())
    >>> class some_test(tcfl.tc.tc_c):
    >>>     ...
    """
    tags = {}
    zephyr_vars = set([ 'ZEPHYR_BASE', 'ZEPHYR_GCC_VARIANT',
                        'ZEPHYR_TOOLCHAIN_VARIANT' ])
    zephyr_vars_missing = zephyr_vars - set(os.environ.keys())
    if 'ZEPHYR_GCC_VARIANT' in zephyr_vars_missing \
       and 'ZEPHYR_TOOLCHAIN_VARIANT' in set(os.environ.keys()):
        # ZEPHYR_GCC_VARIANT deprecated -- always remove it
        # TOOLCHAIN_VARIANT (the new form) is set
        zephyr_vars_missing.remove('ZEPHYR_GCC_VARIANT')
    if zephyr_vars_missing:
        tags['skip'] = ",".join(zephyr_vars_missing) + " not exported"
    return tags


def console_dump_on_failure(testcase, alevel = 0):
    """
    If a testcase has errored, failed or blocked, dump the consoles of
    all the targets.

    :param tcfl.tc.tc_c testcase: testcase whose targets' consoles we
      want to dump

    Usage: in a testcase's teardown function:

    >>> import tcfl.tc
    >>> import tcfl.tl
    >>>
    >>> class some_test(tcfl.tc.tc_c):
    >>>     ...
    >>>
    >>>     def teardown_SOMETHING(self):
    >>>         tcfl.tl.console_dump_on_failure(self)
    """
    assert isinstance(testcase, tcfl.tc.tc_c)
    if not testcase.result_eval.failed \
       and not testcase.result_eval.errors \
       and not testcase.result_eval.blocked:
        return
    for target in list(testcase.targets.values()):
        if not hasattr(target, "console"):
            continue
        attachments = {}
        console_list = target.console.list()
        if len(console_list) == 1:
            attachments["console"] = target.console.generator_factory(None)
        else:
            for console in console_list:
                attachments['console[' + console + ']'] = \
                    target.console.generator_factory(console)
        if testcase.result_eval.failed:
            target.report_fail("console dump due to failure",
                               attachments, alevel = alevel)
        elif testcase.result_eval.errors:
            target.report_error("console dump due to errors",
                                attachments, alevel = alevel)
        else:
            target.report_blck("console dump due to blockage",
                               attachments, alevel = alevel)

def target_ic_kws_get(target, ic, keyword, default = None):
    target.report_info(
        "DEPRECATED: tcfl.tl.target_ic_kws_get() deprecated in"
        " favour of target.ic_key_get()",
        dict(trace = traceback.format_stack()))
    return target.ic_key_get(ic, keyword, default)


def setup_verify_slip_feature(zephyr_client, zephyr_server, _ZEPHYR_BASE):
    """
    The Zephyr kernel we use needs to support
    CONFIG_SLIP_MAC_ADDR, so if any of the targets needs SLIP
    support, make sure that feature is Kconfigurable
    Note we do this after building, because we need the full
    target's configuration file.

    :param tcfl.tc.target_c zephyr_client: Client Zephyr target

    :param tcfl.tc.target_c zephyr_server: Client Server target

    :param str _ZEPHYR_BASE: Path of Zephyr source code

    Usage: in a testcase's setup methods, before building Zephyr code:

    >>>     @staticmethod
    >>>     def setup_SOMETHING(zephyr_client, zephyr_server):
    >>>         tcfl.tl.setup_verify_slip_feature(zephyr_client, zephyr_server,
                                                  tcfl.tl.ZEPHYR_BASE)

    Look for a complete example in
    :download:`../examples/test_network_linux_zephyr_echo.py`.
    """
    assert isinstance(zephyr_client, tcfl.tc.target_c)
    assert isinstance(zephyr_server, tcfl.tc.target_c)
    client_cfg = zephyr_client.zephyr.config_file_read()
    server_cfg = zephyr_server.zephyr.config_file_read()
    slip_mac_addr_found = False
    for file_name in [
            os.path.join(_ZEPHYR_BASE, "drivers", "net", "Kconfig"),
            os.path.join(_ZEPHYR_BASE, "drivers", "slip", "Kconfig"),
    ]:
        if os.path.exists(file_name):
            with open(file_name, "r") as f:
                if "SLIP_MAC_ADDR" in f.read():
                    slip_mac_addr_found = True

    if ('CONFIG_SLIP' in client_cfg or 'CONFIG_SLIP' in server_cfg) \
       and not slip_mac_addr_found:
        raise tcfl.tc.blocked_e(
            "Can't test: your Zephyr kernel in %s lacks support for "
            "setting the SLIP MAC address via configuration "
            "(CONFIG_SLIP_MAC_ADDR) -- please upgrade"
            % _ZEPHYR_BASE, dict(dlevel = -1)
        )

def teardown_targets_power_off(testcase):
    """
    Power off all the targets used on a testcase.

    :param tcfl.tc.tc_c testcase: testcase whose targets we are to
      power off.

    Usage: in a testcase's teardown function:

    >>> import tcfl.tc
    >>> import tcfl.tl
    >>>
    >>> class some_test(tcfl.tc.tc_c):
    >>>     ...
    >>>
    >>>     def teardown_SOMETHING(self):
    >>>         tcfl.tl.teardown_targets_power_off(self)

    Note this is usually not necessary as the daemon will power off
    the targets when cleaning them up; usually when a testcase fails,
    you want to keep them on to be able to inspect them.
    """
    assert isinstance(testcase, tcfl.tc.tc_c)
    for dummy_twn, target  in reversed(list(testcase.targets.items())):
        target.power.off()

def tcpdump_enable(ic):
    """
    Ask an interconnect to capture IP traffic with TCPDUMP

    Note this is only possible if the server to which the interconnect
    is attached has access to it; if the interconnect is based on the
    :class:vlan_pci driver, it will support it.

    Note the interconnect *must be* power cycled after this for the
    setting to take effect. Normally you do this in the *start* method
    of a multi-target testcase

    >>> def start(self, ic, server, client):
    >>>    tcfl.tl.tcpdump_enable(ic)
    >>>    ic.power.cycle()
    >>>    ...
    """
    assert isinstance(ic, tcfl.tc.target_c)
    ic.property_set('tcpdump', ic.kws['tc_hash'] + ".cap")


def tcpdump_collect(ic, filename = None):
    """
    Collects from an interconnect target the tcpdump capture

    .. warning: this will power off the interconnect!

    :param tcfl.tc.target_c ic: interconnect target
    :param str filename: (optional) name of the local file where to
        copy the tcpdump data to; defaults to
        *report-RUNID:HASHID-REP.tcpdump* (where REP is the repetition
        count)
    """
    assert isinstance(ic, tcfl.tc.target_c)
    assert filename == None or isinstance(filename, str)
    if filename == None:
        filename = \
            "report-%(runid)s:%(tc_hash)s" % ic.kws \
            + "-%d" % (ic.testcase.eval_count + 1) \
            + ".tcpdump"
    ic.power.off()		# ensure tcpdump flushes
    ic.store.dnload(ic.kws['tc_hash'] + ".cap", filename)
    ic.report_info("tcpdump available in file %s" % filename)


_os_release_regex = re.compile("^[_A-Z]+=.*$")

def linux_os_release_get(target, prefix = ""):
    """
    Get the os-release file from a Linux target and return its
    contents as a dictionary.

    /etc/os-release is documented in
    https://www.freedesktop.org/software/systemd/man/os-release.html

    :param tcfl.tc.target_c target: target on which to run (must be
      started and running a Linux OS)
    :returns: dictionary with the */etc/os-release* values, such as:

      >>> os_release = tcfl.tl.linux_os_release_get(target)
      >>> print os_release
      >>> { ...
      >>>     'ID': 'fedora',
      >>>     'VERSION_ID': '29',
      >>>   ....
      >>> }
    """
    os_release = {}
    output = target.shell.run("cat %s/etc/os-release || true" % prefix,
                              output = True, trim = True)
    # parse painfully line by line, this way it might be better at
    # catching corruption in case we had output from kernel or
    # whatever messed up in the output of the command
    for line in output.split("\n"):
        line = line.strip()
        if not _os_release_regex.search(line):
            continue
        field, value = line.strip().split("=", 1)
        # remove leading and ending quotes
        os_release[field] = value.strip('"')

    target.kw_set("linux.distro", os_release['ID'].strip('"'))
    target.kw_set("linux.distro_version", os_release['VERSION_ID'].strip('"'))
    return os_release


def linux_mount_scratchfs(target,
                          reformat: bool = True, path: str = "/scratch"):
    """
    Mount in the target the TCF-scratch filesystem in */scratch*

    The default partitioning schemas define a partition with a label
    TCF-scratch that is available to be reformated and reused at will
    by any automation. This is made during deployment.

    This function creates an ext4 filesystem on it and mounts it in
    */scratch* if not already mounted.

    :param tcfl.tc.target_c target: target on which to mount

    :param bool reformat: (optional; default *True*) re-format the
      scratch file system before mounting it

    :param str path: (optional; default */scratch*) path where to
      mount the scratch file system.
    """
    output = target.shell.run("cat /proc/mounts", output = True, trim = True)
    if ' /scratch ' not in output:
        # not mounted already
        if reformat:
            target.shell.run("mkfs.ext4 -F /dev/disk/by-partlabel/TCF-scratch")
        target.shell.run(f"mkdir -p {path}")
        target.shell.run(f"mount /dev/disk/by-partlabel/TCF-scratch {path}")


def linux_ssh_root_nopwd(target, prefix = ""):
    """
    Configure a SSH deamon to allow login as root with no passwords

    .. _howto_restart_sshd:

    In a script:

    >>> tcfl.tl.linux_ssh_root_nopwd(target)
    >>> tcfl.tl.linux_sshd_restart(ic, target)

    or if doing it by hand, wait for *sshd* to be fully ready; it is a hack:

    >>> target.shell.run("systemctl restart sshd")
    >>> target.shell.run(           # wait for sshd to fully restart
    >>>     # this assumes BASH
    >>>     "while ! exec 3<>/dev/tcp/localhost/22; do"
    >>>     " sleep 1s; done", timeout = 10)

    - why not *nc*? easy and simple; not default installed in most distros

    - why not *curl*? most distros have it installed; if SSH is replying
      with the SSH-2.0 string, then likely the daemon is ready

      Recent versions of curl now check for HTTP headers, so can't be
      really used for this

    - why not plain *ssh*? because that might fail by many other
      reasons, but you can check the debug in *ssh -v* messages for a
      *debug1: Remote protocol version* string; output is harder to
      keep under control and *curl* is kinda faster, but::

        $ ssh -v localhost 2>&1 -t echo | fgrep -q 'debug1: Remote protocol version'

      is a valid test

    - why not *netstat*? for example::

        $  while ! netstat -antp | grep -q '^tcp.*:22.*LISTEN.*sshd'; do sleep 1s; done

      *netstat* is not always available, when available, that is also
       a valid test

    Things you can do after this:

    1. switch over to an SSH console if configured (they are faster
       and depending on the HW, more reliable):

       >>> target.console.setup_preferred()

    """
    target.shell.run('mkdir -p %s/etc/ssh' % prefix)
    target.shell.run(
        f'grep -qe "^PermitRootLogin yes" {prefix}/etc/ssh/sshd_config'
        f' || echo "PermitRootLogin yes" >> {prefix}/etc/ssh/sshd_config')
    target.shell.run(
        f'grep -qe "^PermitEmptyPasswords yes" {prefix}/etc/ssh/sshd_config'
        f' || echo "PermitEmptyPasswords yes" >> {prefix}/etc/ssh/sshd_config')


def deploy_linux_ssh_root_nopwd(_ic, target, _kws):
    linux_ssh_root_nopwd(target, "/mnt")


def linux_hostname_set(target, prefix = ""):
    """
    Set the target's OS hostname to the target's name

    :param tcfl.tc.target_c target: target where to run

    :param str prefix: (optional) directory where the root partition
      is mounted.
    """
    target.shell.run("echo %s > %s/etc/hostname" % (target.id, prefix))

def deploy_linux_hostname_set(_ic, target, _kws):
    linux_hostname_set(target, "/mnt")

def linux_sshd_restart(ic, target):
    """
    Restart SSHD in a linux/systemctl system

    Use with :func:`linux_ssh_root_nopwd`
    """
    target.tunnel.ip_addr = target.addr_get(ic, "ipv4")
    target.shell.run("systemctl restart sshd")
    target.shell.run(		# wait for sshd to fully restart
        # this assumes BASH
        "while ! exec 3<>/dev/tcp/localhost/22; do"
        " sleep 1s; done", timeout = 15)
    time.sleep(2)	# SSH settle
    # force the SSH tunnel on 22 being re-created -- since it might be
    # toast...bit it will distirb ithir thrids? we just restarted
    # sshd. They were disturbed
    last_e = None
    for _count in range(4):
        try:
            target.tunnel.remove(22)
            target.ssh.check_call("echo Checking SSH tunnel is up")
            break
        except tcfl.error_e as e:
            last_e = e
            data = e.attachments
            target.report_info(
                f"SSH tunnel not up: SSH returned {data['returncode']}",
                e.attachments)
            continue
    else:
        raise last_e


def linux_ipv4_addr_get_from_console(target, ifname):
    """
    Get the IPv4 address of a Linux Interface from the Linux shell
    using the *ip addr show* command.

    :param tcfl.tc.target_c target: target on which to find the IPv4
      address.
    :param str ifname: name of the interface for which we want to find
      the IPv4 address.

    :raises tcfl.tc.error_e: if it cannot find the IP address.

    Example:

    >>> import tcfl.tl
    >>> ...
    >>>
    >>> @tcfl.tc.interconnect("ipv4_addr")
    >>> @tcfl.tc.target("pos_capable")
    >>> class my_test(tcfl.tc.tc_c):
    >>>    ...
    >>>    def eval(self, tc, target):
    >>>        ...
    >>>        ip4 = tcfl.tl.linux_ipv4_addr_get_from_console(target, "eth0")
    >>>        ip4_config = target.addr_get(ic, "ipv4")
    >>>        if ip4 != ip4_config:
    >>>            raise tcfl.tc.failed_e(
    >>>                "assigned IPv4 addr %s is different than"
    >>>                " expected from configuration %s" % (ip4, ip4_config))

    """
    output = target.shell.run("ip addr show dev %s" % ifname, output = True)
    regex = re.compile(r"^    inet (?P<name>([0-9\.]+){4})/", re.MULTILINE)
    matches = regex.search(output)
    if not matches:
        raise tcfl.tc.error_e("can't find IP addr")
    return matches.groupdict()['name']

def sh_export_proxy(ic, target):
    """
    If the interconnect *ic* defines a proxy environment, issue a
    shell command in *target* to export environment variables that
    configure it:

    >>> class test(tcfl.tc.tc_c):
    >>>
    >>>     def eval_some(self, ic, target):
    >>>         ...
    >>>         tcfl.tl.sh_export_proxy(ic, target)

    would yield a command such as::

       $ export  http_proxy=http://192.168.98.1:8888 \
          https_proxy=http://192.168.98.1:8888 \
          no_proxy=127.0.0.1,192.168.98.1/24,fd:00:62::1/104 \
          HTTP_PROXY=$http_proxy \
          HTTPS_PROXY=$https_proxy \
          NO_PROXY=$no_proxy

    being executed in the target

    """
    proxy_cmd = ""
    proxy_hosts = {}
    if 'http_proxy' in ic.kws:
        target.shell.run("export http_proxy=%(http_proxy)s; "
                          "export HTTP_PROXY=$http_proxy" % ic.kws)
        proxy_hosts['http_proxy'] = ic.kws['http_proxy']
    if 'https_proxy' in ic.kws:
        target.shell.run("export https_proxy=%(https_proxy)s; "
                         "export HTTPS_PROXY=$https_proxy" % ic.kws)
        proxy_hosts['https_proxy'] = ic.kws['https_proxy']
    if proxy_hosts:
        # if we are setting a proxy, make sure it doesn't do the
        # local networks
        no_proxyl = [ "127.0.0.1", "localhost" ]
        if 'ipv4_addr' in ic.kws:
            no_proxyl += [ "%(ipv4_addr)s/%(ipv4_prefix_len)s" ]
        if 'ipv6_addr' in ic.kws:
            no_proxyl += [ "%(ipv6_addr)s/%(ipv6_prefix_len)s" ]
        proxy_cmd += " no_proxy=" + ",".join(no_proxyl)
        target.shell.run("export " + proxy_cmd % ic.kws)
        target.shell.run("export NO_PROXY=$no_proxy")
    return proxy_hosts

def sh_proxy_environment(ic, target, prefix = "/"):
    """
    If the interconnect *ic* defines a proxy environment, issue
    commands to write the proxy configuration to the target's
    */etc/environment*.

    As well, if the directory */etc/apt/apt.conf.d* exists in the
    target, an APT proxy configuration file is created there with the
    same values.

    See :func:`tcfl.tl.sh_export_proxy`
    """
    apt_proxy_conf = []
    dnf_proxy = None

    # FIXME: we need to change proxies in targets to be homed in the
    # proxy hierarchy as a backup? they are always network specific anyway?
    proxy_hosts = {}

    if 'ftp_proxy' in ic.kws:
        target.shell.run(
            "echo -e 'ftp_proxy=%(ftp_proxy)s\nFTP_PROXY=%(ftp_proxy)s'"
            " >> /etc/environment"
            % ic.kws)
        apt_proxy_conf.append('FTP::proxy "%(ftp_proxy)s";' % ic.kws)
        proxy_hosts['ftp_proxy'] = ic.kws['ftp_proxy']

    if 'http_proxy' in ic.kws:
        target.shell.run(
            "echo -e 'http_proxy=%(http_proxy)s\nHTTP_PROXY=%(http_proxy)s'"
            " >> /etc/environment"
            % ic.kws)
        apt_proxy_conf.append('HTTP::proxy "%(http_proxy)s";' % ic.kws)
        dnf_proxy = f"{ic.kws['http_proxy']}"	# default to HTTP proxy
        proxy_hosts['http_proxy'] = ic.kws['http_proxy']

    if 'https_proxy' in ic.kws:
        target.shell.run(
            "echo -e 'https_proxy=%(https_proxy)s\nHTTPS_PROXY=%(https_proxy)s'"
            " >> /etc/environment"
            % ic.kws)
        apt_proxy_conf.append('HTTPS::proxy "%(https_proxy)s";' % ic.kws)
        dnf_proxy = f"{ic.kws['https_proxy']}"	# override https if available
        proxy_hosts['https_proxy'] = ic.kws['https_proxy']

    if 'no_proxy' in ic.kws:
        target.shell.run("echo 'export NO_PROXY=%(no_proxy)s"
                         " no_proxy=%(no_proxy)s' >> ~/.bashrc" % ic.kws)
    if apt_proxy_conf:
        target.shell.run(
            "test -d /etc/apt/apt.conf.d"
            " && cat > /etc/apt/apt.conf.d/tcf-proxy.conf <<EOF\n"
            "Acquire {\n"
            + "\n".join(apt_proxy_conf) +
            "}\n"
            "EOF")

    # there is no way to distinguis https vs http so we need to make a
    # wild guess by overriding
    if dnf_proxy:
        target.shell.run(
            "rm -f /tmp/dnf.conf; test -r /etc/dnf/dnf.conf"
            # sed's -n and -i don't play well, so copy it to post-process
            f" && cp /etc/dnf/dnf.conf /tmp/dnf.conf"
            # sed: wipe existing proxy (if any) add new setting
            # hack: assumes [main] section is the only one
            f" && sed -n -e '/^proxy=/!p' -e '$aproxy={dnf_proxy}' /tmp/dnf.conf > /etc/dnf/dnf.conf")

    return proxy_hosts


def linux_wait_online(ic, target, loops = 20, wait_s = 0.5):
    """
    Wait on the serial console until the system is assigned an IP

    We make the assumption that once the system is assigned the IP
    that is expected on the configuration, the system has upstream
    access and thus is online.
    """
    assert isinstance(target, tcfl.tc.target_c)
    assert isinstance(ic, tcfl.tc.target_c) \
        and "interconnect_c" in ic.kws['interfaces'], \
        "argument 'ic' shall be an interconnect/network target"
    assert loops > 0
    assert wait_s > 0
    target.shell.run(
        "for i in {1..%d}; do"
        " hostname -I | grep -Fq %s && break;"
        " date +'waiting %.1f @ %%c';"
        " sleep %.1fs;"
        "done; "
        "hostname -I "
        "# block until the expected IP is assigned, we are online"
        % (loops, target.addr_get(ic, "ipv4"), wait_s, wait_s),
        timeout = (loops + 1) * wait_s)


def linux_wait_host_online(target, hostname, loops = 20):
    """
    Wait on the console until the given hostname is pingable

    We make the assumption that once the system is assigned the IP
    that is expected on the configuration, the system has upstream
    access and thus is online.
    """
    assert isinstance(target, tcfl.tc.target_c)
    assert isinstance(hostname, str)
    assert loops > 0
    target.shell.run(
        "for i in {1..%d}; do"
        " ping -c 3 %s && break;"
        "done; "
        "# block until the hostname pongs"
        % (loops, hostname),
        # three pings at one second each
        timeout = (loops + 1) * 3 * 1)


def linux_rsync_cache_lru_cleanup(target, path, max_kbytes):
    """Cleanup an LRU rsync cache in a path in the target

    An LRU rsync cache is a file tree which is used as an accelerator
    to rsync trees in to the target for the POS deployment system;

    When it grows too big, we need to purge the files/dirs that were
    uploaded longest ago (as this indicates when it was the last time
    they were used). For that we use the mtime and we sort by it.

    Note this is quite naive, since we can't really calculate well the
    space occupied by directories, which adds to the total...

    So it sorts by reverse mtime (newest first) and iterates over the
    list until the accumulated size is more than max_kbytes; then it
    starts removing files.

    """
    assert isinstance(target, tcfl.tc.target_c)
    assert isinstance(path, str)
    assert max_kbytes > 0

    testcase = target.testcase
    target.report_info(
        "rsync cache: reducing %s to %dMiB" % (path, max_kbytes / 1024.0))

    prompt_original = target.shell.prompt_regex
    python_error_ex = target.console.text(
        re.compile("^(.*Error|Exception):.*^>>> ", re.MULTILINE | re.DOTALL),
        name = "python error",
        timeout = 0, poll_period = 1,
        raise_on_found = tcfl.tc.error_e("error detected in python"))
    testcase.expect_tls_append(python_error_ex)
    try:
        target.send("TTY=dumb python || python2 || python3")	 # launch python!
        # This lists all the files in the path recursively, sorting
        # them by oldest modification time first.
        #
        # In Python? Why? because it is much faster than doing it in
        # shell when there are very large trees with many
        # files. Make sure it is 2 and 3 compat.
        #
        # Note we are feeding lines straight to the python
        # interpreter, so we need an extra newline for each
        # indented block to close them.
        #
        # The list includes the mtime, the size and the name  (not using
        # bisect.insort() because it doesn't support an insertion key
        # easily).
        #
        # Then start iterating newest first until the total
        # accumulated size exceeds what we have been
        # asked to free and from there on, wipe all files.
        #
        # Note we list directories after the files; since
        # sorted(), when sorted by mtime is most likely they will
        # show after their contained files, so we shall be able to
        # remove empty dirs. Also, sorted() is stable. If they
        # were actually needed, they'll be brought back by rsync
        # at low cost.
        #
        # We use statvfs() to get the filesystem's block size to
        # approximate the actual space used in the disk
        # better. Still kinda naive.
        #
        # why not use scandir()? this needs to be able to run in
        # python2 for older installations.
        #
        # walk: walk depth first, so if we rm all the files in a dir,
        # the dir is empty and we will wipe it too after wiping
        # the files; if stat fails with FileNotFoundError, that's
        # usually a dangling symlink; ignore it. OSError will
        # likely be something we can't find, so we ignore it too.
        #
        # And don't print anything...takes too long for large trees
        target.shell.run("""
import os, errno, stat
l = []
dirs = []
try:
    fsbsize = os.statvfs('%(path)s').f_bsize
    for r, dl, fl in os.walk('%(path)s', topdown = False):
        for fn in fl + dl:
            fp = os.path.join(r, fn)
            try:
                s = os.stat(fp)
                sd = fsbsize * ((s.st_size + fsbsize - 1) / fsbsize)
                l.append((s.st_mtime, sd, fp, stat.S_ISDIR(s.st_mode)))
            except (OSError, FileNotFoundError) as x:
                pass
except (OSError, FileNotFoundError) as x:
    pass


acc = 0
sc = %(max_bytes)d
for e in sorted(l, key = lambda e: e[0], reverse = True):
    acc += e[1]
    if acc > sc:
        if e[3]:
            try:
                os.rmdir(e[2])
            except OSError as x:
                if x.errno == errno.ENOTEMPTY:
                    pass
        else:
            os.unlink(e[2])


exit()""" % dict(path = path, max_bytes = max_kbytes * 1024))
    finally:
        target.shell.prompt_regex = prompt_original
        testcase.expect_tls_remove(python_error_ex)

#
# Well, so this is a hack anyway; we probably shall replace this with
# a combination of:
#
# - seeing if the progress counter is updating
# - a total timeout dependent on the size of the package
#

#:
#: Timeouts for adding different, big size bundles
#:
#: To add to this configuration, specify in a client configuration
#: file or on a test script:
#:
#: >>> tcfl.tl.swupd_bundle_add_timeouts['BUNDLENAME'] = TIMEOUT
#:
#: note timeout for adding a bundle defaults to 240 seconds.
swupd_bundle_add_timeouts = {
    # Keep this list alphabetically sorted!
    'LyX': 500,
    'R-rstudio': 1200, # 1041MB
    'big-data-basic': 800, # (1049MB)
    'c-basic': 500,
    'computer-vision-basic': 800, #1001MB
    'container-virt': 800, #197.31MB
    'containers-basic-dev': 1200, #921MB
    'database-basic-dev': 800, # 938
    'desktop': 480,
    'desktop-dev': 2500,	# 4500 MiB
    'desktop-autostart': 480,
    'desktop-kde-apps': 800, # 555 MB
    'devpkg-clutter-gst': 800, #251MB
    'devpkg-gnome-online-accounts': 800, # 171MB
    'devpkg-gnome-panel': 800, #183
    'devpkg-nautilus': 800, #144MB
    'devpkg-opencv': 800, # 492MB
    'education': 800,
    'education-primary' : 800, #266MB
    'game-dev': 6000, # 3984
    'games': 800, # 761MB
    'java-basic': 1600, # 347MB
    'java9-basic': 1600, # 347MB
    'java11-basic': 1600,
    'java12-basic': 1600,
    'java13-basic': 1600,
    'machine-learning-basic': 1200, #1280MB
    'machine-learning-tensorflow': 800,
    'machine-learning-web-ui': 1200, # (1310MB)
    'mail-utils-dev ': 1000, #(670MB)
    'maker-cnc': 800, # (352MB)
    'maker-gis': 800, # (401MB)
    'network-basic-dev': 1200, #758MB
    'openstack-common': 800, # (360MB)
    'os-clr-on-clr': 8000,
    'os-clr-on-clr-dev': 8000,	# quite large too
    'os-core-dev': 800,
    'os-testsuite': 1000,
    'os-testsuite-phoronix': 2000,	# 4000MiB
    'os-testsuite-phoronix-desktop': 1000,
    'os-testsuite-phoronix-server': 1000,
    'os-util-gui': 800, #218MB
    'os-utils-gui-dev': 6000, #(3784MB)
    'python-basic-dev': 800, #466MB
    'qt-basic-dev': 2400, # (1971MB)
    'service-os-dev': 800, #652MB
    'storage-cluster': 800, #211MB
    'storage-util-dev': 800, # (920MB)
    'storage-utils-dev': 1000, # 920 MB
    'supertuxkart': 800, # (545 MB)
    'sysadmin-basic-dev': 1000, # 944 MB
    'texlive': 1000, #1061
}

def swupd_bundle_add(ic, target, bundle_list,
                     debug = None, url = None,
                     wait_online = True, set_proxy = True,
                     fix_time = None, add_timeout = None,
                     become_root = False):
    """Install bundles into a Clear distribution

    This is a helper that install a list of bundles into a Clear
    distribution taking care of a lot of the hard work.

    While it is preferrable to have an open call to *swupd bundle-add*
    and it should be as simple as that, we have found we had to
    repeatedly take manual care of many issues and thus this helper
    was born. It will take take care of:

    - wait for network connectivity [convenience]
    - setup proxy variables [convenience]
    - set swupd URL from where to download [convenience]
    - fix system's time for SSL certification (in *broken* HW)
    - retry bundle-add calls when they fail due:
      - random network issues
      - issues such as::

          Error: cannot acquire lock file. Another swupd process is \
          already running  (possibly auto-update)

      all retryable after a back-off wait.

    :param tcfl.tc.target_c ic: interconnect the target uses for
      network connectivity

    :param tcfl.tc.target_c target: target on which to operate

    :param bundle_list: name of the bundle to add or list of them;
      note they will be added each in a separate *bundle-add* command

    :param bool debug: (optional) run *bundle-add* with ``--debug--``;
      if *None*, defaults to environment *SWUPD_DEBUG* being defined
      to any value.

    :param str url: (optional) set the given *url* for the swupd's
      repository with *swupd mirror*; if *None*, defaults to
      environment *SWUPD_URL* if defined, otherwise leaves the
      system's default setting.

    :param bool wait_online: (optional) waits for the system to have
      network connectivity (with :func:`tcfl.tl.linux_wait_online`);
      defaults to *True*.

    :param bool set_proxy: (optional) sets the proxy environment with
      :func:`tcfl.tl.sh_export_proxy` if the interconnect exports proxy
      information; defaults to *True*.

    :param bool fix_time: (optional) fixes the system's time if *True*
      to the client's time.; if *None*, defaults to environment
      *SWUPD_FIX_TIME* if defined, otherwise *False*.

    :param int add_timeout: (optional) timeout to set to wait for the
      *bundle-add* to complete; defaults to whatever is configured in
      the :data:`tcfl.tl.swupd_bundle_add_timeouts` or the the default
      of 240 seconds.

    :param bool become_root: (optional) if *True* run the command as super
      user using *su* (defaults to *False*). To be used when the script has the
      console logged in as non-root.

      This uses *su* vs *sudo* as some installations will not install
      *sudo* for security reasons.

      Note this function assumes *su* is configured to work without
      asking any passwords. For that, PAM module *pam_unix.so* has to
      be configured to include the option *nullok* in target's files
      such as:

      - */etc/pam.d/common-auth*
      - */usr/share/pam.d/su*

      ``tcf-image-setup.sh`` will do this for you if using it to set
      images.
    """

    testcase = target.testcase

    # gather parameters / defaults & verify
    assert isinstance(ic, tcfl.tc.target_c)
    assert isinstance(target, tcfl.tc.target_c)
    if isinstance(bundle_list, str):
        bundle_list = [ bundle_list ]
    else:
        assert isinstance(bundle_list, collections.Iterable) \
            and all(isinstance(item, str) for item in bundle_list), \
            "bundle_list must be a string (bundle name) or list " \
            "of bundle names, got a %s" % type(bundle_list).__name__

    if debug == None:
        debug = 'SWUPD_DEBUG' in os.environ
    else:
        assert isinstance(debug, bool)

    if url == None:
        url = os.environ.get('SWUPD_URL', None)
    else:
        assert isinstance(url, str)

    if fix_time == None:
        fix_time = os.environ.get("SWUPD_FIX_TIME", None)
    else:
        assert isinstance(fix_time, bool)

    # note add_timeout might be bundle-specific, so we can't really
    # set it here
    if add_timeout != None:
        assert add_timeout > 0
    assert isinstance(become_root, bool)

    # the system's time is untrusted; we need it to be correct so the
    # certificate verification works--set it from the client's time
    # (assumed to be correct). Use -u for UTC settings to avoid TZ
    # issues
    if fix_time:
        target.shell.run("date -us '%s'; hwclock -wu"
                         % str(datetime.datetime.utcnow()))

    if wait_online:		        # wait for connectivity to be up
        tcfl.tl.linux_wait_online(ic, target)

    kws = dict(
        debug = "--debug" if debug else "",
        hashid = testcase.kws['tc_hash']
    )
    if become_root:
        kws['su_prefix'] = "su -mc '"
        kws['su_postfix'] = "'"
    else:
        kws['su_prefix'] = ""
        kws['su_postfix'] = ""
    target.shell.run(			# fix clear certificates if needed
        "%(su_prefix)s"			# no space here, for su -mc 'COMMAND'
        "certs_path=/etc/ca-certs/trusted;"
        "if [ -f $certs_path/regenerate ]; then"
        " rm -f $certs_path/regenerate $certs_path/lock;"
        " clrtrust -v generate;"
        "fi"
        "%(su_postfix)s"		# no space here, for su -mc 'COMMAND'
        % kws)

    if set_proxy:			# set proxies if needed
        tcfl.tl.sh_export_proxy(ic, target)

    if url:				# set swupd URL if needed
        kws['url'] = url
        target.shell.run("%(su_prefix)sswupd mirror -s %(url)s%(su_postfix)s"
                         % kws)

    # Install them bundles
    #
    # installing can take too much time, so we do one bundle at a
    # time so the system knows we are using the target.
    #
    # As well, swupd doesn't seem to be able to recover well from
    # network glitches--so we do a loop where we retry a few times;
    # we record how many tries we did and the time it took as KPIs
    for bundle in bundle_list:
        kws['bundle'] = bundle
        # adjust bundle add timeout
        # FIXME: add patch to bundle-add to recognize --dry-run --sizes so
        # that it lists all the bundles it has to download and their sizes
        # so we can dynamically adjust this
        if add_timeout == None:
            if bundle in swupd_bundle_add_timeouts:
                _add_timeout = swupd_bundle_add_timeouts[bundle]
                target.report_info(
                    "bundle-add: adjusting timeout to %d per configuration "
                    "tcfl.tl.swupd_bundle_add_timeouts" % _add_timeout)
            else:
                _add_timeout = 240
        else:
            _add_timeout = add_timeout

        count = 0
        top = 10
        for count in range(1, top + 1):
            # WORKAROUND: server keeps all active
            target.testcase.targets_active()
            # We use -p so the format is the POSIX standard as
            # defined in
            # https://pubs.opengroup.org/onlinepubs/009695399/utilities
            # /time.html
            # STDERR section
            output = target.shell.run(
                "time -p"
                " %(su_prefix)sswupd bundle-add %(debug)s %(bundle)s%(su_postfix)s"
                " || echo FAILED''-%(hashid)s"
                % kws,
                output = True, timeout = _add_timeout)
            if not 'FAILED-%(tc_hash)s' % testcase.kws in output:
                # We assume it worked
                break
            if 'Error: Bundle too large by' in output:
                df = target.shell.run("df -h", output = True, trim = True)
                du = target.shell.run("du -hsc /persistent.tcf.d/*",
                                      output = True, trim = True)
                raise tcfl.tc.blocked_e(
                    "swupd reports rootfs out of space to"
                    " install bundle %(bundle)s" % kws,
                    dict(output = output, df = df, du = du))
            target.report_info("bundle-add: failed %d/%d? Retrying in 5s"
                               % (count, top))
            time.sleep(5)
        else:
            # match below's
            target.report_data("swupd bundle-add retries",
                               bundle, count)
            raise tcfl.tc.error_e("bundle-add failed too many times")

        # see above on time -p
        kpi_regex = re.compile(r"^real[ \t]+(?P<seconds>[\.0-9]+)$",
                               re.MULTILINE)
        m = kpi_regex.search(output)
        if not m:
            raise tcfl.tc.error_e(
                "Can't find regex %s in output" % kpi_regex.pattern,
                dict(output = output))
        # maybe domain shall include the top level image type
        # (clear:lts, clear:desktop...)
        target.report_data("swupd bundle-add retries",
                           bundle, int(count))
        target.report_data("swupd bundle-add duration (seconds)",
                           bundle, float(m.groupdict()['seconds']))

def linux_time_set(target):
    """
    Set the time in the target using the controller's date as a reference

    :param tcfl.tc.target_c target: target whose time is to be set

    """
    target.shell.run("date -us '%s'; hwclock -wu --noadjfile"
                     % str(datetime.datetime.utcnow()))


def linux_package_add(ic, target, *packages,
                      timeout = 120, fix_time = True,
                      proxy_wait_online = True,
                      **kws):
    """Ask target to install Linux packages in distro-generic way

    This function checks the target to see what it has installed and
    then uses the right tool to install the list of packages; distro
    specific package lists can be given:

    >>> tcfl.tl.linux_package_add(
    >>>      ic, target, [ 'git', 'make' ],
    >>>      centos = [ 'cmake' ],
    >>>      ubuntu = [ 'cmake' ])

    :param tcfl.tc.target_c ic: interconnect that provides *target*
      with network connectivity

    :param tcfl.tc.target_c target: target where to install

    :param list(str) packages: (optional) list of packages to install

    :param list(str) DISTROID: (optonal) list of packages to install,
      in addition to *packages* specific to a distro:

       - CentOS: use *centos*
       - Clear Linux OS: use *clear*
       - Fedora: use *fedora*
       - RHEL: use *rhel*
       - Ubuntu: use *ubuntu*

    :param bool proxy_wait_online: (optional, default *True*) if there
      are proxies defined, wait for them to be pingable before
      accessing the network.

    FIXME:

     - missing support for older distros and to specify packages
       for specific distro version

     - most of the goodes in swupd_bundle_add have to be moved here,
       like su/sudo support, ability to setup proxy, fix date and pass
       distro-specific setups (like URLs, etc)
    """
    assert isinstance(ic, tcfl.tc.target_c)
    assert isinstance(target, tcfl.tc.target_c)
    assert all(isinstance(package, str) for package in packages), \
            "package list must be a list of strings;" \
            " some items in the list are not"
    for key, packagel in kws.items():
        assert isinstance(packagel, list), \
            "value %s must be a list of strings; found %s" \
            % (key, type(packagel))
        assert all(isinstance(package, str) for package in packages), \
            "value %s must be a list of strings;" \
            " some items in the list are not" % key

    if not 'linux.distro' in target.kws or not 'linux.distro_version' in target.kws:
        os_release = linux_os_release_get(target)
        distro = os_release['ID']
        distro_version = os_release['VERSION_ID']
        target.kw_set("linux.distro", distro)
        target.kw_set("linux.distro_version", distro_version)
    else:
        distro = target.kws['linux.distro']
        distro_version = target.kws['linux.distro_version']

    if fix_time:
        # if the clock is messed up, SSL signing won't work for some things
        target.shell.run("date -us '%s'; hwclock -wu"
                         % str(datetime.datetime.utcnow()))

    if proxy_wait_online:
        proxy_hosts = set()
        if 'ftp_proxy' in ic.kws:
            proxy_hosts.add(urllib.parse.urlparse(ic.kws['ftp_proxy']).hostname)
        if 'http_proxy' in ic.kws:
            proxy_hosts.add(urllib.parse.urlparse(ic.kws['http_proxy']).hostname)
        if 'https_proxy' in ic.kws:
            proxy_hosts.add(urllib.parse.urlparse(ic.kws['https_proxy']).hostname)
        if proxy_hosts:
            target.report_info(
                f"waiting for proxies to be online (ping): {', '.join(proxy_hosts)}")
            for hostname in proxy_hosts:
                linux_wait_host_online(target, hostname)

    packages = list(packages)
    if distro.startswith('clear'):
        _packages = packages + kws.get("any", []) + kws.get("clear", [])
        if _packages:
            tcfl.tl.swupd_bundle_add(ic, target, _packages,
                                     add_timeout = timeout,
                                     fix_time = True, set_proxy = True)
    elif distro == 'centos':
        _packages = packages + kws.get("any", []) + kws.get("centos", [])
        if _packages:
            target.shell.run("dnf install -qy " +  " ".join(_packages),
            timeout = timeout)
    elif distro == 'fedora':
        _packages = packages + kws.get("any", []) + kws.get("fedora", [])
        if _packages:
            target.shell.run(
                "dnf install --releasever %s -qy " % distro_version
                +  " ".join(_packages),
                timeout = timeout)
    elif distro == 'rhel':
        _packages = packages + kws.get("any", []) + kws.get("rhel", [])
        if _packages:
            target.shell.run("dnf install -qy " +  " ".join(_packages),
                             timeout = timeout)
    elif distro == 'ubuntu':
        _packages = packages + kws.get("any", []) + kws.get("ubuntu", [])
        if _packages:
            # FIXME: add needed repos [ubuntu|debian]_extra_repos
            target.shell.run(
                "sed -i 's/main restricted/main restricted universe multiverse/'"
                " /etc/apt/sources.list")
            target.shell.run("apt-get -qy update", timeout = timeout)
            target.shell.run(
                "DEBIAN_FRONTEND=noninteractive"
                " apt-get install -qy " +  " ".join(_packages),
                timeout = timeout)
    else:
        raise tcfl.tc.error_e("unknown OS: %s %s (from /etc/os-release)"
                              % (distro, distro_version))
    return distro, distro_version

def linux_network_ssh_setup(ic, target, proxy_wait_online = True):
    """
    Ensure the target has network and SSH setup and running

    :param tcfl.tc.target_c ic: interconnect where the target is connected

    :param tcfl.tc.target_c target: target on which to operate

    :param bool proxy_wait_online: (optional, default *True*) if there
      are proxies defined, wait for them to be pingable before
      accessing the network.
    """
    tcfl.tl.linux_wait_online(ic, target)
    tcfl.tl.sh_export_proxy(ic, target)
    tcfl.tl.sh_proxy_environment(ic, target)

    # Make sure the SSH server is installed
    distro, distro_version = tcfl.tl.linux_package_add(
        ic, target,
        centos = [ 'openssh-server' ],
        clear = [ 'sudo', 'openssh-server', 'openssh-client' ],
        fedora = [ 'openssh-server' ],
        rhel = [ 'openssh-server' ],
        ubuntu = [ 'openssh-server' ],
        proxy_wait_online = proxy_wait_online
    )

    tcfl.tl.linux_ssh_root_nopwd(target)	# allow remote access
    tcfl.tl.linux_sshd_restart(ic, target)

tap_mapping_result_c = {
    'ok': tcfl.result_c(passed = 1),
    'not ok': tcfl.result_c(failed = 1),
    'skip': tcfl.result_c(skipped = 1),
    'todo': tcfl.result_c(errors = 1),
}

def tap_parse_output(output_itr):
    """
    Parse `TAP
    <https://testanything.org/tap-version-13-specification.html>`_
    into a dictionary

    :param str output: TAP formatted output
    :returns: dictionary keyed by test subject containing a dictionary
       of key/values:
       - lines: list of line numbers in the output where data was found
       - plan_count: test case number according to the TAP plan
       - result: result of the testcase (ok or not ok)
       - directive: if any directive was found, the text for it
       - output: output specific to this testcase
    """
    tap_version = re.compile("^TAP version (?P<tap_version>[0-9]+)$")
    tc_plan = re.compile(r"^(?P<plan_min>[0-9]+)\.\.(?P<plan_max>[0-9]+)$")
    tc_line = re.compile(r"^(?P<result>(ok |not ok ))(?P<plan_count>[0-9]+ )?"
                         r"-\w*(?P<subject>[^#]*)?(#(?P<directive>.*))?$")
    tc_output = re.compile(r"^#(?P<data>.*)$")
    skip_regex = re.compile(r"skip(ped)?:?", re.IGNORECASE)
    todo_regex = re.compile(r"todo:?", re.IGNORECASE)

    # state
    _plan_min = None
    _plan_top = None
    plan_set_at = None
    tcs = {}

    linecnt = 0
    _plan_count = 1
    plan_max = 0
    tc = None
    for line in output_itr:
        linecnt += 1
        m = tc_plan.search(line)
        if m:
            if plan_set_at and _plan_count > plan_max:
                # only complain if we have not completed it, otherwise
                # consider it spurious and ignore
                continue
            if plan_set_at:
                raise tcfl.tc.blocked_e(
                    f"{linecnt}: setting range, but was already set at {plan_set_at}",
                    dict(line_count = linecnt, line = line))
            plan_set_at = linecnt
            plan_min = int(m.groupdict()['plan_min'])
            plan_max = int(m.groupdict()['plan_max'])
            continue
        m = tc_line.search(line)
        if m:
            d = m.groupdict()
            result = d['result']
            count = d['plan_count']
            if not count or count == "":
                count = _plan_count	# if no count, use our internal one
            subject = d['subject']
            if not subject or subject == "":
                subject = str(count)	# if no subject, use count
            subject = subject.strip()
            directive_s = d.get('directive', '')
            if directive_s == None:
                directive_s = ''
                # directive is "TODO [text]", "skip: [text]"
            directive_s = directive_s.strip()
            directive_sl = directive_s.split()
            if directive_sl:
                directive = directive_sl[0]
                if skip_regex.match(directive):
                    result = "skip"
                elif todo_regex.match(directive):
                    result = "todo"
            else:
                directive = ''
            tc_current = subject
            print(f"DEBUG subject is {subject}")
            tcs[subject] = dict(
                lines = [ linecnt ],
                plan_count = count,
                result = result.strip(),
                directive = directive_s,
                output = "",
            )
            tc = tcs[subject]
            # oficially a new testcase in the plan
            _plan_count += 1
            continue
        m = tap_version.search(line)
        if m:
            d = m.groupdict()
            tap_version = int(d['tap_version'])
            if tap_version < 12:
                raise RuntimeError("%d: Can't process versions < 12", linecnt)
            continue
        m = tc_output.search(line)
        if m:
            d = m.groupdict()
            if tc:
                tc['output'] += d['data'] + "\n"
                tc['lines'].append(linecnt)
            else:
                raise tcfl.tc.blocked_e(
                    "Can't parse output; corrupted? didn't find a header",
                    dict(output = output_itr, line = linecnt))
            continue
    return tcs


# FIXME: this should declare target is tcfl.tc.target_c but it can't
# yet because we have an import hell that will be fixed in v0.16
# - add to target: tcfl.tc.target_c, component: str
def rpyc_connect(target, component: str,
                 cert_name: str = "default",
                 iface_name = "power", sync_timeout = 60):
    """Connect to an RPYC component exposed by the target

    :param tcfl.tc.target_c target: target which exposes the RPYC
      component.

      An RPYC component exposes in the inventory for the (power)
      interface two fields:

        - *interfaces.power.COMPONENT.rpyc_port*: (int) TCP port in
          the server where the RPYC listens

        - *interfaces.power.COMPONENT.ssl_enabled*: (bool) *True* if
          SSL enabled; SSL is considered disabled otherwise.

    :param str component: name of the component that exposes the RPYC
      interface.

    :param str cert_name: (optional, defaults to *default*) name of
      the client certificate to use to connect to the RPYC
      interface it requests SSL.

      See :mod:`ttbl.certs` for more info; the server can issue SSL
      client certificates to use as One-Time-Passwords for the
      duration of the target allocation.

    :param str iface_name: (optional; default *power*) name of the
      interface which exposes the component. In most cases it is the
      power interface, but it could be associated to any.


    :param int sync_timeout: (optional; default *60* seconds) timeout
      for calls to remote functions to return. Increase when running
      longer functions, although this will incur a longter time to
      detect network drops.

      As well, it can be done temporarily as:

      >>> remote = tcfl.tl.rpyc_connect(...)
      >>> ...
      >>> timeout_orig = remote._config['sync_request_timeout']
      >>> try:
      >>>     remote._config['sync_request_timeout'] = 30 * 60 # 30min
      >>>     ... run long remote operation...
      >>> finally:
      >>>     remote._config['sync_request_timeout'] = timeout_orig
    """
    # FIXME: assert isinstance(target, tcfl.tc.target_c)
    assert isinstance(component, str)
    assert isinstance(cert_name, str)

    try:
        import rpyc	# pylint: disable=import-outside-toplevel
    except ImportError:
        tcfl.tc.tc_global.report_blck(
            "MISSING MODULES: install them with: pip install --user rpyc")
        raise

    rpyc_port = target.kws[f"interfaces.{iface_name}.{component}.rpyc_port"]
    ssl_enabled = target.kws[f"interfaces.{iface_name}.{component}.ssl_enabled"]
    if ssl_enabled:
        # get the certificate files from the server, unless they are already created
        client_key_path = os.path.join(target.tmpdir, "client." + cert_name + ".key")
        client_cert_path = os.path.join(target.tmpdir, "client." + cert_name + ".cert")

        # FIXME: this needs to be smarter -- it needs to re-download
        # if the allocid is different; now it is causing way too many
        # issues when there are files left around (eg: reusing tmp).
        if True or not os.path.isfile(client_key_path) or not os.path.isfile(client_cert_path):
            # if we have the certificates int
            r = target.certs.get(cert_name)
            with open(client_key_path, "w") as keyf:
                keyf.write(r['key'])
            with open(client_cert_path, "w") as certf:
                certf.write(r['cert'])

        target.report_info(
            f"rpyc: SSL-connecting (cert '{cert_name}') to '{component}' on"
            f" {target.rtb.parsed_url.hostname}:{rpyc_port}", dlevel = 3)
        target.report_info(
            f"rpyc: using key/cert path {client_key_path}'", dlevel = 4)
        remote = rpyc.utils.classic.ssl_connect(
            target.rtb.parsed_url.hostname,
            port = rpyc_port,
            keyfile = client_key_path,
            certfile =  client_cert_path,
            ssl_version = ssl.PROTOCOL_TLS
        )
        target.report_info(
            f"rpyc: SSL-connected (cert '{cert_name}') to '{component}' on"
            f" {target.rtb.parsed_url.hostname}:{rpyc_port}", dlevel = 2)
    else:
        target.report_info(
            f"rpyc: connecting to '{component}' on"
            f" {target.rtb.parsed_url.hostname}:{rpyc_port}", dlevel = 3)
        remote = rpyc.utils.classic.ssl_connect(
            target.rtb.parsed_url.hostname,
            port = rpyc_port)
        target.report_info(
            f"rpyc: connected to '{component}' on"
            f" {target.rtb.parsed_url.hostname}:{rpyc_port}", dlevel = 2)

    if sync_timeout:
        assert isinstance(sync_timeout, int) and sync_timeout > 0, \
            "sync_timeout: expected positive number of seconds; got {sync_timeout}"
        remote._config['sync_request_timeout'] = sync_timeout
    return remote
