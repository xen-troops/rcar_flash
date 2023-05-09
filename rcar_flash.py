#!/usr/bin/env python3

import sys
import os
import serial
import serial.tools.list_ports
import logging
import yaml
import argparse
import pathlib
import traceback
import time
from typing import List
from string import printable

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
cpld_available = False


def main():
    parser = argparse.ArgumentParser(
        description='Automatic RCar Gen3/Gen4 flash tool', )
    parser.add_argument(
        '--conf',
        help='Name of config file. Default is "rcar_flash.yaml"',
        type=argparse.FileType('r'),
        default='rcar_flash.yaml')

    subparsers = parser.add_subparsers(required=True, dest="action")

    parser_flash = subparsers.add_parser(
        name="flash",
        help="Flash loaders onto a board",
        epilog=
        'Each loader has format loader_name[:file_name], by default file name is taken from YAML configuration file'
    )
    parser_list_loaders = subparsers.add_parser(
        name="list-loaders", help="List supported loaders for a board")
    parser_list_boards = subparsers.add_parser(name="list-boards",
                                               help="List supported boards")
    parser_flash.add_argument(
        '-c',
        '--cpld',
        action='store',
        metavar='serial_no',
        nargs='?',
        type=str,
        default=None,
        const="AUTO",
        help='Use CPLD tool to automatically switch a board to a flash mode')

    parser_flash.add_argument(
        '-f',
        '--flash-writer',
        metavar="flashwriter.mot",
        nargs='?',
        type=str,
        default=None,
        const="DEFAULT",
        help=
        'Upload Flash Writer before trying to flash a board. Default name is read from YAML configuration file'
    )

    parser_flash.add_argument('-p',
                              '--path',
                              type=pathlib.Path,
                              default='.',
                              help='Path where loaders are located')

    parser_flash.add_argument('-s', '--serial', help='Serial console to use')

    parser_flash.add_argument('-b',
                              '--board',
                              type=str,
                              required=True,
                              help='Board name')

    parser_list_loaders.add_argument('-b',
                                     '--board',
                                     type=str,
                                     required=True,
                                     help='Board name')

    parser_flash.add_argument(
        'loaders',
        metavar='loaders',
        nargs='+',
        help='List of loaders to flash or "all" to flash all')

    args = parser.parse_args()

    actions = {
        "list-loaders": do_list_loaders,
        "list-boards": do_list_boards,
        "flash": do_flash,
    }

    if args.action not in actions:
        raise Exception(f"Unknown action {args.action}")

    config = read_config(args.conf)
    actions[args.action](config, args)


def read_config(stream):
    return yaml.load(stream, Loader=yaml.Loader)


def get_board(conf, board_name):
    if board_name not in conf["board"]:
        raise Exception(
            f"Board {board_name} is not found in the provided configuration file"
        )
    log.info(f"Reading config for board {board_name}")
    return conf["board"][board_name]


def do_list_loaders(conf, args):
    board = get_board(conf, args.board)
    row_format = "{:<24}     {:<15}     {:35}      {:10}"
    header = row_format.format("Loader", "Flash address", "Default file",
                               "Flash target")
    print(header)
    print("-" * len(header))
    ipls = board["ipls"]
    loaders = [(x, f'0x{ipls[x]["flash_addr"]:x}', ipls[x]["file"],
                ipls[x]["flash_target"]) for x in ipls.keys()]
    # Sort twice. First time by address...
    loaders.sort(key=lambda x: int(x[1], 16))
    # And second time - by "flash_target" attribute
    loaders.sort(key=lambda x: x[3])
    for l in loaders:
        print(row_format.format(*l))


def do_list_boards(conf, args):
    row_format = "{:<15}     {:>15}"
    print(row_format.format("Board Name", "Default flash loader file"))
    boards = sorted(conf["board"].keys())
    for b in boards:
        print(row_format.format(b, conf["board"][b]["flash_writer"]))


def do_flash(conf, args):
    board = get_board(conf, args.board)

    # Do some sanity checks before tring to flash anything
    # Build list of loaders
    loaders: dict[str, str] = dict()
    l: str
    for l in args.loaders:
        if l == "all":
            if len(args.loaders) > 1:
                raise Exception(
                    "You can either use 'all' or define list of loaders")
            for k in board["ipls"].keys():
                loaders[k] = os.path.join(args.path, board["ipls"][k]["file"])
                if not os.path.exists(loaders[k]):
                    raise Exception(
                        f"File {loaders[k]} for loader {k} does not exists!")

        else:
            if ':' in l:
                idx = l.index(":")
                ipl_name = l[:idx]
                if ipl_name not in board["ipls"]:
                    raise Exception(f"Unknown loader name: {ipl_name}")
                ipl_file = l[idx + 1:]
            else:
                ipl_name = l
                if ipl_name not in board["ipls"]:
                    raise Exception(f"Unknown loader name: {ipl_name}")
                ipl_file = os.path.join(args.path,
                                        board["ipls"][ipl_name]["file"])
            if not os.path.exists(ipl_file):
                raise Exception(
                    f"File {ipl_file} for loader {ipl_name} does not exists!")
            loaders[ipl_name] = ipl_file

    log.info("We are going to flash the following loaders")
    log.info("---")
    for l in loaders.keys():
        log.info(f"{l:24} : {loaders[l]}")
    log.info("---")

    # Check if need to nudge CPLD
    if args.cpld:
        if not cpld_available:
            raise Exception("pyftdi is not available")
        if "cpld_profile" not in board:
            raise Exception(
                "'cpld_profile' is not set for board, can't control CPLD")

        # Force enable uploading flash_writer, otherwise it have no sense
        # to use CPLD
        if not args.flash_writer:
            args.flash_writer = "DEFAULT"

        cpld_profile = conf["cpld_profiles"][board["cpld_profile"]]
        dev_serial = cpld_determine_serial(cpld_profile, args)
        args.cpld = dev_serial
        cpld = cpld_get_instance(dev_serial, cpld_profile)
        cpld.check_rev()
        cpld.serial_mode()
        cpld.reset()
        time.sleep(0.5)
        # Need to release port, so pyserial can use it
        del cpld

    conn = open_connection(board, args)
    # Upload flash writer if needed
    if args.flash_writer:
        if not args.cpld:
            log.info("Please ensure that board is in the serial download mode")
        if args.flash_writer == "DEFAULT":
            fname = board["flash_writer"]
        else:
            fname = args.flash_writer
        if not os.path.exists(fname):
            raise Exception(f"Flash write file {fname} does not exists!")
        log.info(f"Sending flash writer file {fname}...")
        send_flashwriter(board, fname, conn)
        if "sup_baud" in board:
            # Increase comm speed if SUP command is available
            conn_send(conn, "sup\r")
            conn.close()
            conn = open_connection(board, args, use_sup=True)
    else:
        log.info("Please ensure that board is in Monitor mode")

    # Upload files one by one
    for k in loaders.keys():
        ipl_entry = board["ipls"][k]
        addr = ipl_entry["flash_addr"]
        flash_target = conf["flash_target"][ipl_entry["flash_target"]]
        log.info(
            f"Writing {k} ({loaders[k]}) at 0x{addr:x} using {ipl_entry['flash_target']}"
        )
        flash_one_loader(conn, loaders[k], addr, flash_target)

    conn.close()

    log.info("All done!")
    if args.cpld:
        cpld = cpld_get_instance(dev_serial, cpld_profile)
        cpld.normal_mode()
        cpld.reset()
    else:
        log.info("You might need to reboot your board")


def send_data_with_progress(data, conn: serial.Serial, print_progress=True):
    bytes_sent = 0
    total = len(data)
    if print_progress:
        # start output of the progress from the new line
        # to avoid overwriting of the previous info in some cases
        print("")
    while bytes_sent < total:
        to_send = min(total-bytes_sent, 10*1024)
        conn.write(data[bytes_sent : bytes_sent+to_send])
        bytes_sent += to_send
        if print_progress:
            print(f"{bytes_sent:_}/{total:_} ({bytes_sent * 100 // total}%)", end="\r")
    if print_progress:
        # send "newline char" to start further output on the new line
        print("")


def flash_one_loader(conn, fname, flash_addr, flash_target):
    conn_send(conn, "\r")

    orig_timeout: int
    for evt in flash_target["sequence"]:
        if "timeout" in evt:
            # set required timeout
            orig_timeout = conn.timeout
            conn.timeout = evt["timeout"]
        conn_wait_for(conn, evt["wait_for"])
        if "timeout" in evt:
            # restore original timeout
            conn.timeout = orig_timeout
        if evt["send"] == "img_addr":
            conn_send(conn, f"{get_srec_load_addr(fname)}\r")
        elif evt["send"] == "file_size":
            conn_send(conn, f"{os.path.getsize(fname):X}\r")
        elif evt["send"] == "flash_addr":
            conn_send(conn, f"{flash_addr:X}\r")
        elif evt["send"] == "const":
            conn_send(conn, f"{evt['val']}")
        elif evt["send"] == "file":
            with open(fname, "rb") as f:
                data = f.read()
                send_data_with_progress(data, conn)
        else:
            raise Exception(f"Unknown value to send: {evt['send']}")
    conn_wait_for(conn, ">")


def send_flashwriter(board_conf, fname: str, conn: serial.Serial):
    with open(fname, "rb") as f:
        data = f.read()
    send_data_with_progress(data, conn)
    conn_wait_for(conn, ">")


def open_connection(board_conf, args, use_sup=False):
    # Default value
    dev_name = '/dev/ttyUSB0'
    if args.serial:
        dev_name = args.serial
    elif args.cpld is not None and args.cpld != "AUTO":
        serial_no = args.cpld
        for serial_port in serial.tools.list_ports.comports():
            if serial_port.serial_number == serial_no:
                dev_name = serial_port.device
                break
        else:
            raise Exception(
                f"Can't find device with serial number {serial_no}")
    if use_sup and "sup_baud" in board_conf:
        # use SUP if requested and available
        baud = board_conf["sup_baud"]
    elif "baud" in board_conf:
        baud = board_conf["baud"]
    else:
        baud = 115200

    log.info(f"Using serial port {dev_name} with baudrate {baud}")
    conn = serial.Serial(port=dev_name, baudrate=baud, timeout=20)
    if conn.is_open:
        conn.close()
    conn.open()

    return conn


def conn_wait_for(conn, expect: str):
    rcv_str = ""
    while expect not in rcv_str:
        data = conn.read(1)
        if not data:
            raise TimeoutError(f"Timeout waiting for `{expect}` from the device")
        rcv_char = chr(data[0])
        if rcv_char in printable or rcv_char == '\b':
            print(rcv_char, end='', flush=True)
        rcv_str += rcv_char


def conn_send(conn, data):
    conn.write(data.encode("ascii"))


def get_srec_load_addr(fname):
    with open(fname, "r") as f:
        lines = f.readlines()
    for l in lines[:20]:
        if l.startswith("S3"):
            return l[4:12]
    raise Exception(f"Could not read srec load address (S3) from {fname}")


# CPLD Code begins there
try:
    import pyftdi
    cpld_available = True
except ModuleNotFoundError:
    log.error("pyftdi module not found. CPLD Functinality is disabled.")
    log.error("   Please install the module.")
    log.error("   You can try \"pip3 install --user pyftdi\"")
    log.error(
        "   Or use your favourite packet manager (\"apt install python3-ftdi\" perhaps?)"
    )


def cpld_determine_serial(cpld_profile, args) -> str:
    import pyftdi.usbtools
    # Try to determine USB device serial number where CPLD resides

    # We are luck: user provided serial number
    if args.cpld != "AUTO":
        return args.cpld

    # If the exact serial number of the port is not specified for CPLD
    # then we have the following steps for automatic detection:
    # 1. list USB devices with VID:PID provided in the CPLD profile,
    #    if only one device is found - let's use it;
    # 2. if more than one devices are present with the same VID:PID
    #    and we have a device specified by user (e.g.: -s /dev/ttyUSB*)
    #    then we will try to get a serial number for that user-provided
    #    device, if it has requested VID:PID;
    # 3. if nothing works - print out the list of found devices with
    #    the required VID:PID and ask the user to select one.

    # Try to make a best effort basing on CPLD profile
    usb_vid = cpld_profile["usb_vid"]
    usb_pid = cpld_profile["usb_pid"]
    devices = pyftdi.usbtools.UsbTools.find_all([(usb_vid, usb_pid)])
    if not devices:
        raise Exception(
            f"Could not find any USB devices with VID:PID = {usb_vid:04X}:{usb_pid:04X}"
        )

    if len(devices) == 1:
        # see case 1 above
        return devices[0][0].sn

    if len(devices) > 1 and args.serial:
        # see case 2 above
        for serial_port in serial.tools.list_ports.comports(include_links = True):
            if serial_port.device == args.serial and serial_port.serial_number:
                for device in devices:
                    if device[0].sn == serial_port.serial_number:
                        log.info(f"Will use {serial_port.device} --> {serial_port.serial_number}")
                        return serial_port.serial_number

    # see case 3 above
    log.info(f"Multiple devices are available with VID:PID = {usb_vid:04X}:{usb_pid:04X}")
    for device in devices:
        log.info(f"{device[0].sn}:  {device[0].description}")
    log.info(f"Please specify exactly which one to use, with option '--cpld XXXXXXXX'.")

    raise Exception(
        "Could not find suitable device."
        )


def cpld_get_instance(dev_serial, cpld_profile):
    if cpld_profile["protocol"] == "i2c":
        cpld = cpld_i2c(dev_serial, cpld_profile)
    elif cpld_profile["protocol"] == "spi":
        cpld = cpld_spi(dev_serial, cpld_profile)
    else:
        raise Exception(f"Unknown CPLD protocol '{cpld_profile['protocol']}'")
    return cpld


class cpld_i2c:
    SDA_PIN = 1 << 7
    SCL_PIN = 1 << 6
    STATE_LOW = -1
    STATE_HIGH = 1
    STATE_SAME = 0

    def __init__(self, serial: str, profile: dict):
        import pyftdi.gpio
        gpio = pyftdi.gpio.GpioAsyncController()
        gpio.configure(f'ftdi://ftdi:2232h:{serial}/2', direction=0, initial=0)
        self._gpio = gpio
        self._profile = profile
        self._devaddr = profile["dev_addr"]

    def __del__(self):
        self._gpio.close()

    def reset(self):
        self.write_cmd("reset")

    def serial_mode(self):
        self.write_cmd("serial_mode")

    def normal_mode(self):
        self.write_cmd("normal_mode")

    def check_rev(self):
        if "revision" not in self._profile:
            log.info("CPLD profile does not provide means to check revision")
            return
        log.info("CPLD: Checking revision")
        cmd_conf = self._profile["revision"]
        reg_addr = cmd_conf["reg"]
        expected_rev = cmd_conf["expected"]
        revision: list = self._read_reg(self._devaddr, reg_addr,
                                        len(expected_rev))
        log.info("Read revision: %s",
                 "".join([f"{x:02X}" for x in reversed(revision)]))
        if expected_rev != revision:
            raise Exception(
                f"Board revision mismatch. Expected {expected_rev} got {revision}"
            )

    def write_cmd(self, cmd):
        log.info("CPLD: Issuing command %s", cmd)
        cmd_conf = self._profile[cmd]
        reg_addr = cmd_conf["reg"]
        reg_data = cmd_conf["write"]
        self._write_regs(self._devaddr, reg_addr, reg_data)

    def _sleep(self):
        time.sleep(0.0001)

    def _set_state(self, sda, scl):
        # We rely on external pullup when we want to output HIGH state
        direction = self._gpio.direction
        pins = 0
        if sda == self.STATE_LOW:
            direction |= self.SDA_PIN
            pins |= self.SDA_PIN
        elif sda == self.STATE_HIGH:
            direction &= ~self.SDA_PIN
            pins |= self.SDA_PIN

        if scl == self.STATE_LOW:
            direction |= self.SCL_PIN
            pins |= self.SCL_PIN
        elif sda == self.STATE_HIGH:
            direction &= ~self.SCL_PIN
            pins |= self.SCL_PIN

        self._gpio.set_direction(pins, direction)

    def _get_state(self, read_sda, read_scl):
        pins = 0
        if read_sda:
            pins |= self.SDA_PIN
        if read_scl:
            pins |= self.SCL_PIN
        self._gpio.set_direction(pins, 0)
        return self._gpio.read()

    def _start_cond(self):
        # Initial state
        self._set_state(self.STATE_HIGH, self.STATE_HIGH)
        self._wait_scl()

        # Actual start condition - SDA goes low with SCL being high
        self._set_state(self.STATE_LOW, self.STATE_HIGH)
        self._sleep()

    def _stop_cond(self):
        # Previous state
        self._set_state(self.STATE_LOW, self.STATE_HIGH)
        self._sleep()
        self._wait_scl()
        self._sleep()
        # Actual stop condition - SDA goes high while SCL being high
        self._set_state(self.STATE_HIGH, self.STATE_HIGH)

    def _wait_scl(self):
        # Wait while SCL becomes HIGH
        while self._get_state(False, True) & self.SCL_PIN == 0:
            pass

    def _write_bit(self, bit):
        if bit:
            self._set_state(self.STATE_HIGH, self.STATE_LOW)
        else:
            self._set_state(self.STATE_LOW, self.STATE_LOW)

        self._set_state(self.STATE_SAME, self.STATE_HIGH)
        self._sleep()
        self._wait_scl()
        self._set_state(self.STATE_SAME, self.STATE_LOW)
        self._sleep()

    def _read_bit(self):
        self._set_state(self.STATE_HIGH, self.STATE_LOW)
        self._sleep()
        self._set_state(self.STATE_HIGH, self.STATE_HIGH)
        self._sleep()
        self._wait_scl()
        data = self._get_state(True, False) & self.SDA_PIN
        self._set_state(self.STATE_HIGH, self.STATE_LOW)
        self._sleep()
        return 1 if data else 0

    def _write_byte(self, byte):
        log.debug("I2C: Write byte: %02X", byte)
        for i in range(7, -1, -1):
            self._write_bit(byte & (1 << i))
        ack = self._read_bit()
        if ack != 0:
            raise Exception("Got NAK during CPLD communication")

    def _read_byte(self, ack):
        ret = 0

        for i in range(7, -1, -1):
            ret |= self._read_bit() << i

        self._write_bit(ack)
        return ret

    def _read_reg(self, dev_addr, reg_addr, reg_len):
        self._start_cond()
        self._write_byte(dev_addr & 0xFE)

        self._write_byte(reg_addr >> 8)
        self._write_byte(reg_addr & 0xFF)

        self._start_cond()
        self._write_byte(dev_addr | 0x1)

        ret = []
        for i in range(reg_len):
            data = self._read_byte(i == reg_len - 1)
            log.debug("I2C read: 0x%X", data)
            ret.append(data)
        self._stop_cond()
        return ret

    def _write_regs(self, dev_addr, reg_addr, reg_data):
        self._start_cond()
        self._write_byte(dev_addr & 0xFE)

        self._write_byte(reg_addr >> 8)
        self._write_byte(reg_addr & 0xFF)

        for reg in reg_data:
            self._write_byte(reg)
        self._stop_cond()


class cpld_spi:
    CS_PIN = 0x8
    MOSI_PIN = 0x40
    MISO_PIN = 0x10
    SCK_PIN = 0x04

    def __init__(self, serial: str, profile: dict):
        import pyftdi.gpio
        gpio = pyftdi.gpio.GpioSyncController()
        gpio.configure(f'ftdi://ftdi:232r:{serial}/1',
                       direction=self.CS_PIN | self.MOSI_PIN | self.SCK_PIN,
                       initial=0,
                       frequency=57600)
        self._gpio = gpio
        self._profile = profile
        self._sync()

    def __del__(self):
        self._gpio.close()

    def reset(self):
        self.write_cmd("reset")

    def serial_mode(self):
        self.write_cmd("serial_mode")

    def normal_mode(self):
        self.write_cmd("normal_mode")

    def check_rev(self):
        log.info("CPLD SPI protocol does not support revision check")

    def write_cmd(self, cmd):
        log.info("CPLD: Issuing command %s", cmd)
        cmd_conf = self._profile[cmd]
        reg_addr = cmd_conf["reg"]
        reg_data = cmd_conf["write"]
        self._write_reg(reg_addr, reg_data)

    def _sleep(self):
        time.sleep(0.00001)

    def _prepare_write_byte(self, byte):
        cmd = []
        for i in range(7, -1, -1):
            if byte & (1 << i):
                mosi = self.MOSI_PIN
            else:
                mosi = 0
            cmd.append(mosi | self.CS_PIN | self.SCK_PIN)
            cmd.append(mosi | self.CS_PIN)
        return cmd

    def _sync(self):
        cmd = [self.SCK_PIN, 0]
        for _ in range(32):
            cmd.append(self.CS_PIN | self.SCK_PIN)
            cmd.append(self.CS_PIN)
        cmd.append(self.CS_PIN | self.SCK_PIN | self.MOSI_PIN)
        cmd.append(self.CS_PIN | self.MOSI_PIN)
        self._gpio.exchange(cmd)
        self._sleep()
        cmd = self._prepare_write_byte(0xfe)
        cmd.append(self.SCK_PIN | self.MOSI_PIN)
        cmd.append(self.MOSI_PIN)
        self._gpio.exchange(cmd)

    def _write_reg(self, addr, reg):
        cmd = [self.SCK_PIN, 0]
        for i in range(3, -1, -1):
            cmd.extend(self._prepare_write_byte(reg >> (i * 8)))

        self._gpio.exchange(cmd)
        self._sleep()

        cmd = self._prepare_write_byte(addr)
        cmd.append(self.MOSI_PIN | self.SCK_PIN)
        cmd.append(self.MOSI_PIN)
        self._gpio.exchange(cmd)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.fatal(e)
        log.fatal(traceback.format_exc())
