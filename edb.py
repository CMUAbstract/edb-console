#!/usr/bin/python

import sys
import traceback
import os
import time
import atexit
import readline # loading this causes raw_input to offer a rich prompt
import argparse

from pyedb import edb

parser = argparse.ArgumentParser(
            description="EDB Console")
parser.add_argument('--command', '-c',
            help="Run the given command and exit")
parser.add_argument('--stdio', '-o',
            help="File to which to pipe std I/O data relayed from target (default: console)")
args = parser.parse_args()

monitor = None
active_mode = False

if args.stdio is not None:
    CONSOLE_FILE = open(args.stdio, "w")
else:
    CONSOLE_FILE = sys.stdout

def to_int(s):
    if s.startswith("0x"):
        return int(s, 16)
    else:
        return int(s)

def match_keyword(part, words):
    match = None
    for word in words:
        if word.startswith(part):
            if match is not None:
                raise Exception("Ambiguous keyword: " + part)
            match = word
    return match

def print_interrupt_context(context):
    print("Interrupted: %r" % context.type, end='')
    if context.type == "ASSERT":
        print("line: %r" % context.id, end='')
    elif context.type == "BOOT":
        pass
    else:
        print("id: %r" % context.id, end='')
    if context.saved_vcap is not None:
        print("Vcap_saved = %.4f" % context.saved_vcap)
    else:
        print()

def print_watchpoint_event(event):
    print("Watchpoint: id: %r time: %.6f s Vcap = %.4f" % \
            (event.id, event.timestamp, event.vcap))

def init_watchpoint_log(fout):
    fout.write("id,timestamp,vcap\n")
def log_watchpoint_event(fout, event):
    fout.write("%u,%.6f,%.4f\n" %  (event.id, event.timestamp, event.vcap))

def cmd_echo(mon, args):
    print(args)

def cmd_sleep(mon, time_sec):
    time.sleep(float(time_sec))

def cmd_attach(mon, device='/dev/ttyUSB0', uart_log_fname=None):
    global monitor
    monitor = edb.EDB(device=device, uart_log_fname=uart_log_fname)

def cmd_detach(mon):
    mon.destroy()

def cmd_power(mon, state):
    mon.cont_power(state == "on")

def cmd_sense(mon, channel):
    print(mon.sense(channel.upper()))

def cmd_reset(mon):
    mon.reset_debug_mode_state()

def do_stream(mon, out_file, duration_sec, streams, no_parse):
    if duration_sec == "-":
        duration_sec = None # stream indefinitely
    else:
        duration_sec = float(duration_sec)

    streams = [str.upper(s) for s in streams]

    if out_file == "-":
        fp = sys.stdout
        silent = True
    else:
        fp = open(out_file, "w")
        silent = False

    try:
        mon.stream(streams, duration_sec=duration_sec, out_file=fp,
                   silent=silent, no_parse=no_parse)
    except KeyboardInterrupt:
        pass # this is a clean termination

def cmd_stream(mon, out_file, duration_sec, *streams):
    do_stream(mon, out_file, duration_sec, streams=streams, no_parse=False)

def cmd_streamnp(mon, out_file, duration_sec, *streams):
    do_stream(mon, out_file, duration_sec, streams=streams, no_parse=True)

def cmd_charge(mon, target_voltage, method="adc"):
    target_voltage = float(target_voltage)
    if method == "adc":
        vcap = mon.charge(target_voltage)
        print("Vcap = %.4f" % vcap)
    elif method == "cmp":
        mon.charge_cmp(target_voltage)
    else:
        raise Exception("Invalid charger method: " + method)

def cmd_discharge(mon, target_voltage, method="adc"):
    target_voltage = float(target_voltage)
    if method == "adc":
        vcap = mon.discharge(target_voltage)
        print("Vcap = %.4f" % vcap)
    elif method == "cmp":
        mon.discharge_cmp(target_voltage)
    else:
        raise Exception("Invalid charger method: " + method)

def cmd_int(mon):
    global active_mode
    try:
        saved_vcap = mon.interrupt()
        print("Vcap_saved = %.4f" % saved_vcap)
        active_mode = True
    except KeyboardInterrupt:
        pass

def cmd_cont(mon):
    global active_mode
    restored_vcap = mon.exit_debug_mode()
    print("Vcap_restored = %.4f" % restored_vcap)
    active_mode = False

def cmd_ebreak(mon, target_voltage, impl="adc"):
    global active_mode
    target_voltage = float(target_voltage)
    saved_vcap = mon.break_at_vcap_level(target_voltage, impl.upper())
    print("Vcap_saved = %.4f" % saved_vcap)
    active_mode = True

def cmd_break(mon, type, idx, op, energy_level=None):
    idx = int(idx)
    enable = "enable".startswith(op)
    type = match_keyword(type.upper(), edb.host_comm_header.enums['BREAKPOINT_TYPE'].keys())
    energy_level = float(energy_level) if energy_level is not None else None
    mon.toggle_breakpoint(type, idx, enable, energy_level)

def cmd_watch(mon, idx, op, vcap_snapshot="novcap"):
    idx = int(idx)
    enable = "enable".startswith(op)
    vcap_snapshot = "vcap".startswith(vcap_snapshot)
    mon.toggle_watchpoint(idx, enable, vcap_snapshot)

def cmd_wait(mon, log_file=None):
    """Wait to enter active debug mode"""
    global active_mode

    if log_file is not None:
        flog = open(log_file, "w")
        init_watchpoint_log(flog)
    else:
        flog = None

    try:
        start_time = time.time()
        while True:
            event = mon.wait()

            if isinstance(event, edb.InterruptContext):
                print_interrupt_context(event)
                active_mode = True
                break
            if isinstance(event, edb.WatchpointEvent):
                print_watchpoint_event(event)
                if flog is not None:
                    log_watchpoint_event(flog, event)
            elif isinstance(event, edb.StdIOData):
                CONSOLE_FILE.write("%.03f: " % (event.timestamp - start_time))
                CONSOLE_FILE.write(event.string)
                CONSOLE_FILE.flush()
                if event.string[-1] != '\n':
                    CONSOLE_FILE.write('\n')
            elif isinstance(event, edb.EnergyProfile):
                print("%.03f: %r" % (event.timestamp - start_time, event.profile))

    except KeyboardInterrupt:
        pass

def cmd_intctx(mon, source="debugger"):
    source = match_keyword(source.upper(), edb.host_comm_header.enums['INTERRUPT_SOURCE'])
    int_context = mon.get_interrupt_context(source)
    print_interrupt_context(int_context)

def cmd_read(mon, addr, len):
    addr = int(addr, 16)
    len = int(len)
    addr, value = mon.read_mem(addr, len)
    print("0x%08x:" % addr, end='')
    for byte in value:
        print("0x%02x" % byte, end='')
    print()

def cmd_write(mon, addr, *value):
    addr = int(addr, 16)
    value = map(to_int, value)
    mon.write_mem(addr, value)

def cmd_pc(mon):
    print("0x%08x" % mon.get_pc())

def cmd_secho(mon, value):
    value = int(value, 16)
    print("0x%02x" % mon.serial_echo(value))

def cmd_decho(mon, value):
    value = int(value, 16)
    print("0x%02x" % mon.dma_echo(value))

def cmd_replay(mon, file):
    mon.load_replay_log(file)

def cmd_lset(mon, param, value):
    print(mon.set_local_param(param, value))

def cmd_lget(mon, param):
    print(mon.get_local_param(param))

def cmd_rset(mon, param, value):
    print(mon.set_remote_param(param, value))

def cmd_rget(mon, param):
    print(mon.get_remote_param(param))

def cmd_uart(mon, op):
    enable = "enable".startswith(op)
    mon.enable_target_uart(enable)

def cmd_payload(mon, op):
    enable = "enable".startswith(op)
    mon.enable_periodic_payload(enable)

def compose_prompt(active_mode):
    if active_mode:
        return "*> "
    return "> "

cmd_hist_file = os.path.join(os.path.expanduser("~"), ".edb_history")
try:
    readline.read_history_file(cmd_hist_file)
except IOError:
    pass
atexit.register(readline.write_history_file, cmd_hist_file)

while True:

    if args.command is not None:
        once = True
        line = args.command
    else: # read from stdin
        once = False
        try:
            line = input(compose_prompt(active_mode))
        except EOFError:
            print() # print a newline to be nice to the shell
            break
        except KeyboardInterrupt:
            print() # move to next line
            continue

    line = line.strip()
    if len(line) == 0: # new-line character only (blank command)
        continue
    if line.startswith("#"): # comment
        continue
    cmd_lines = line.split(';')
    try:
        for cmd_line in cmd_lines:
            tokens = cmd_line.split()
            cmd = tokens[0]
            glob = globals()
            glob["cmd_" + cmd](monitor, *tokens[1:])
    except Exception as e:
        print(type(e))
        print(traceback.format_exc())

    if once:
        break
