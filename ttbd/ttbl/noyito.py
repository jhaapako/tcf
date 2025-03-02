#! /usr/bin/python3
#
# Copyright (c) 2021 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
# pylint: disable = missing-docstring
"""Drivers for Noyito hardware
---------------------------

This drivers support:

- NOYITO USB 10-Channel 12-Bit AD Data Acquisition Module
  (STM32 UART Communication USB to Serial Chip CH340 ADC Module)

  This device outputs continuousy on the serial port att 115200n81 a
  stream of ASCII like::

    CH0:2016        1.622V
    CH1:2191        1.764V
    CH2:2014        1.624V
    CH3:2193        1.766V
    CH4:2018        1.626V
    CH5:2195        1.769V
    CH6:2018        1.625V
    CH7:2195        1.768V
    CH8:2014        1.622V
    CH9:2194        1.766V

  *formally*::

    CH0:<NNNN><TAB><FLOAT>V\r\n
    ...
    CH8:<NNNN><TAB><FLOAT>V\r\n
    CH9:<NNNN>	<FLOAT>V\r\n
    \r\n

  NNNN being 0000-4095 (12-bit raw sample) and FLOAT (only four bytes)
  0 to 3.3V. Note that when floating disconnected, it always reports
  about 1.7V (because both ground and pins are in the air).

  There is one ground for each bank of five sampling channels.

  The driver for this consists in a power-rail component
  :class:`reader_pc` that starts a multiplexor so multiple readers can
  get the serial output
. For each capture that wants to be
  done, a class:`channel_c` caputer


  This device packs an stm32 MPU, can be reprogrammed, firmware at http://files.banggood.com/2018/06/SKU836210.zip

"""

import logging
import os
import subprocess

import serial

import commonl
import ttbl._install
import ttbl.capture
import ttbl.power

class reader_pc(ttbl.power.daemon_c):
    """
    Implement a multiplexor to read Noyitos' serial port to multiple users

    Noyito reports at 2HZ the value of all the channels on the serial
    port; we will have multiple capturers, belonging to different
    users, taking its output.

    This multiplexor with ncat takes the serial port output and pipes
    it to a Unix domain socket.

    - use ncat because if there is no readers in the domain socket, it
      doesn't even open the serial port

    - supports N readers without conflicts or buffering issues.

    This allows then creating another capture device
    :class:`channel_c`, which just takes the data from a single
    channel (to enforce separation betwene users pullign data from
    separate channels).

    **Target setup**

    This has to be added to the targets's power rail as an explicit
    off component:

    >>> target.interface_impl_add(
    >>>     "power",
    >>>     "data_acquisition_1",
    >>>     ttbl.noyito.reader_pc(
    >>>         "/dev/serial/by-path/pci-0000:00:14.0-usb-0:3.1.6:1.0-port0",
    >>>         explicit = "off"
    >>>     )
    >>> )

    (unfortunately, it lacks a serial number to ease up multiple
    devices), see :class:`commonl.usb_path_by_sibling_late_resolve`
    and similar to map based on other devices with a USB serial #.
    """

    def __init__(self, serial_device, **kwargs):
        assert isinstance(serial_device, str)
        ttbl.power.daemon_c.__init__(
            self,
            cmdline = [
                "/usr/bin/ncat",
                "--listen", "--keep-open",
                "-U", '%(path)s/%(component)s-ncat.socket'
            ],
            check_path = "/usr/bin/ncat",
            **kwargs)
        self.serial_device = serial_device
        self.stdin = None
        self.upid_set(f"Noyito 12-bit 10 channel ADC @{serial_device}",
                      serial_device = serial_device)


    def on(self, target, component):
        # open serial port to set the baud rate, then ncat gets
        # started and it keeps the setting; default is 9600 8n1 no
        # flow control, so we explicitly set what the device needs 115200.
        with serial.Serial(self.serial_device, 115200) as f:
            self.stdin = f
            kws = dict(target.kws)
            kws['name'] = 'ncat'
            kws['component'] = component
            commonl.rm_f(os.path.join(target.state_dir,
                                      f"{component}-ncat.socket"))
            ttbl.power.daemon_c.on(self, target, component)


    def verify(self, target, component, cmdline_expanded):
        kws = dict(target.kws)
        kws.update(self.kws)
        # bring in runtime properties (override the rest)
        kws.update(target.fsdb.get_as_dict())
        kws['component'] = component
        return commonl.process_alive(self.pidfile % kws, self.check_path) != None



class channel_c(ttbl.capture.impl_c):

    def __init__(self, noyito_component, noyito_obj, channels, **kwargs):
        """

        :param str channel_mode: passed straight to :mod:`ttbl.noyito-capture`.

           - *boolean:cutoff=1.3* or *onoff:cutoff=1.3*: interpret
             signal like a boolean value, cutting off at 1.3 Volts

        """
        assert isinstance(noyito_component, str)
        assert isinstance(channels, dict), \
            "channels: expected a dictionary, got %s" % type(channels)

        ttbl.capture.impl_c.__init__(
            self, False, mimetype = "application/json",
            **kwargs)
        self.noyito_component = noyito_component
        self.upid = noyito_obj.upid
        self.capture_program = commonl.ttbd_locate_helper(
            "noyito-capture.py", ttbl._install.share_path,
            log = logging, relsrcpath = ".")
        self.channell = []
        for channel, data in channels.items():
            assert isinstance(channel, int) and channel > 0 and channel <= 10, \
                "channel: channel descriptor has to be an integer 0-10," \
                " got %s" % type(channel)
            # matches ttbd/noyito-capture.py.transform.mode
            mode = data.get('mode', None)
            assert mode in ( None, 'mode', 'bool', 'onoff' ), \
                "channel mode has to be one of: None, mode, bool, onoff; " \
                " got %s" % mode
            name = data.get('mode', str(channel))
            assert isinstance(name, str), \
                "name: expected a string; got %s" % type(name)
            l = [ "%s" % channel ]
            for name, val in data.items():
                l.append("%s=%s" % (name, val))
            self.channell.append(":".join(l))

    def start(self, target, capturer, path):
        # power on the serial port capturer
        target.power.put_on(target, ttbl.who_daemon(),
                            { "component":  self.noyito_component },
                            None, None )

        stream_filename = capturer + ".data.json"
        log_filename = capturer + ".capture.log"
        pidfile = "%s/capture-%s.pid" % (target.state_dir, capturer)

        logf = open(os.path.join(path, log_filename), "w+")
        p = subprocess.Popen(
            [
                "stdbuf", "-e0", "-o0",
                self.capture_program,
                os.path.join(path, stream_filename),
                "%s/%s-ncat.socket" % (target.state_dir, capturer),
            ] + self.channell,
            bufsize = -1,
            close_fds = True,
            shell = False,
            stderr = subprocess.STDOUT, stdout = logf.buffer,
        )

        with open(pidfile, "w+") as pidf:
            pidf.write("%s" % p.pid)
        ttbl.daemon_pid_add(p.pid)

        return True, {
            "default": stream_filename,
            "log": log_filename
        }


    def stop(self, target, capturer, path):
        pidfile = "%s/capture-%s.pid" % (target.state_dir, capturer)
        commonl.process_terminate(pidfile, tag = "capture:" + capturer,
                                  wait_to_kill = 2)
