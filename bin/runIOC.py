#!/usr/bin/env python
import os
import logging
import sys
import argparse

# Twisted boiler-plate code.
from twisted.internet import gireactor
gireactor.install()
from twisted.internet import reactor

# add the project to the python path and inport it
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from softdev import log
from bobcats import ioc

# Setup single argument for verbose logging
parser = argparse.ArgumentParser(description='Run IOC Application')
parser.add_argument('-v', action='store_true', help='Verbose Logging')
parser.add_argument('-d', '--device', type=str, help='Device Name', required=True)
parser.add_argument('--address', type=str, help='Controller address', required=True)
parser.add_argument('--commands', type=int, help='Command Port', required=True)
parser.add_argument('--status', type=int, help='Status Port', required=True)

args = parser.parse_args()
   
# Example of how to start your APP. Modify as needed

if __name__== '__main__':
    if args.v:
        log.log_to_console(logging.DEBUG)
    else:
        log.log_to_console(logging.INFO)

    app = ioc.BobCATSApp(args.device, args.address, args.commands, args.status)  # initialize App
    reactor.addSystemEventTrigger('before', 'shutdown', app.shutdown) # make sure app is properly shutdown
    reactor.run()               # run main-loop

