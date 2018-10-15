import time
import re
from Queue import Queue
from datetime import datetime
from threading import Thread
from enum import Enum
from twisted.internet import reactor
from softdev import epics, models, log
from . import cats
logger = log.get_module_logger(__name__)

NUM_PUCK_SAMPLES = 10
NUM_PLATES = 8
NUM_WELLS = 192
NUM_ROW_WELLS = 24
STATUS_TIME = 0.1


# FIXME: Are these correct?
class ToolType(Enum):
    NONE, LASER, PUCK, PLATE = range(4)


# FIXME: I don't know what types of plates are supported by CATS
class PlateType(Enum):
    TYPE1, TYPE2, TYPE3 = range(3)


class StatusType(Enum):
    IDLE, WAITING, BUSY, ERROR = range(4)


class BobCATS(models.Model):
    connected = models.Enum('CONNECTED', choices=('Inactive', 'Active'), default=0, desc="Robot Connection")
    enabled = models.Enum('ENABLED', choices=('Disabled', 'Enabled'), default=1, desc="Robot Control")
    status = models.Enum('STATUS', choices=StatusType, desc="Robot Status")
    log = models.String('LOG', desc="Sample Operation Message", max_length=1024)
    log_alarm = models.Enum('LOG:ALARM', choices=('INFO', 'WARNING', 'ERROR'), desc="Log Level")
    warning = models.String('WARNING', max_length=40, desc='Warning message')

    # Safety flags
    approach = models.Enum('SAFETY:APPROACH', choices=('OFF', 'ON'), default=0, desc="Robot Approaching")
    prepare = models.Enum('SAFETY:PREPARE', choices=('OFF', 'ON'), default=0, desc="Prepare for Approach")

    # Status
    inputs_fbk = models.BinaryInput('STATE:inputs', desc='Digital Inputs')
    outputs_fbk = models.BinaryInput('STATE:outputs', desc='Digital Outputs')
    power_fbk = models.Enum('STATE:power', choices=('OFF', 'ON'), desc='Robot Power')
    mode_fbk = models.Enum('STATE:auto', choices=('OFF', 'ON'), desc='Auto Mode')
    default_fbk = models.Enum('STATE:default', choices=('OFF', 'ON'), desc='Default Status')
    tool_fbk = models.Enum('STATE:tool', choices=ToolType, desc='Tool Status')
    path_fbk = models.String('STATE:path', max_length=40, desc='Path Name')
    lid_tool_fbk = models.Enum('STATE:toolLid', choices=('NONE', 'LID1', 'LID2', 'LID3'), desc='On tool lid')
    lid_diff_fbk = models.Enum('STATE:diffLid', choices=('NONE', 'LID1', 'LID2', 'LID3'), desc='On Diff lid')
    sample_tool_fbk = models.Integer('STATE:toolSmpl', min_val=0, max_val=NUM_PUCK_SAMPLES*3, desc='On tool Sample')
    sample_diff_fbk = models.Integer('STATE:diffSmpl', min_val=0, max_val=NUM_PUCK_SAMPLES*3, desc='On Diff Sample')
    plate_fbk = models.Integer('STATE:plate', min_val=0, max_val=NUM_PLATES, desc='Plate Status')
    well_fbk = models.Integer('STATE:well', min_val=0, max_val=NUM_WELLS, desc='Well Status')
    barcode_fbk = models.String('STATE:barcode', max_length=40, desc='Barcode Status')
    running_fbk = models.Enum('STATE:running', choices=('OFF', 'ON'), desc='Path Running')
    ln2_dew1_fbk = models.Enum('STATE:D1LN2', choices=('OFF', 'ON'), desc='Dewar 1 LN2')
    ln2_dew2_fbk = models.Enum('STATE:D2LN2', choices=('OFF', 'ON'), desc='Dewar 2 LN2')
    speed_fbk = models.Integer('STATE:speed', min_val=0, max_val=100, units='%', desc='Speed Ratio')
    pucks_dew1_fbk = models.BinaryInput('STATE:pucks1', desc='Puck Detection 1')
    pucks_dew2_fbk = models.BinaryInput('STATE:pucks2', desc='Puck Detection 2')
    pos_dew1_fbk = models.Integer('STATE:pos1', desc='Position Dewar 1')
    pos_dew2_fbk = models.Integer('STATE:pos2', desc='Position Dewar 2')

    mounted_fbk = models.String('STATE:onDiff', max_length=40, desc='Mounted')
    tooled_fbk = models.String('STATE:onTool', max_length=40, desc='Picked')

    #options
    plates_enabled = models.Enum('OPT:plates', choices=('Plates OFF', 'Plates ON'), default=0, desc='Plates Enabled')

    # Params
    next_param = models.String('PAR:nextPort', max_length=40, default='', desc='Port')
    lid_param = models.Enum('PAR:lid', choices=('NONE', 'LID1', 'LID2', 'LID3'), default=0, desc='Selected Lid')
    sample_param = models.Integer('PAR:smpl', min_val=0, max_val=NUM_PUCK_SAMPLES*3, default=0, desc='Selected Sample')
    tool_param = models.Enum('PAR:tool', choices=ToolType, default=2, desc='Selected Tool')
    plate_param = models.Integer('PAR:plate', min_val=0, max_val=NUM_PLATES, desc='Selected Plate')
    well_param = models.Integer('PAR:well', min_val=0, max_val=NUM_WELLS, desc='Selected Well')
    plate_type = models.Enum('PAR:plateType', choices=PlateType, desc='Plate Type')
    plate_drop = models.Integer('PAR:drop', min_val=0, max_val=NUM_PLATES, desc='Plate Drop Place')
    adjust_x = models.Float('PAR:adjustX', units='mm', desc='X Adjust')
    adjust_y = models.Float('PAR:adjustY', units='mm', desc='Y Adjust')
    adjust_z = models.Float('PAR:adjustZ', units='mm', desc='Z Adjust')
    plate_angle = models.Float('PAR:plateAng', units='deg', desc='Plate Angle')

    # exposure params
    start_angle = models.Float('PAR:startAng', units='deg', desc='Start Angle')
    delta_angle = models.Float('PAR:delta', units='deg', desc='Delta Angle')
    exposure = models.Float('PAR:exposure', units='sec', desc='Exposure Time')
    steps_param = models.Integer('PAR:steps', min_val=0, desc='Exposure Steps')
    end_angle = models.Float('PAR:endAng', units='deg', desc='End Angle')

    # Commands
    power_cmd = models.Toggle('CMD:power', zname='Power OFF', oname='Power ON', high=0, desc='Power')
    lid_cmd = models.Toggle('CMD:lid', zname='Open Lid', oname='Close Lid', high=0, desc='Lid Toggle')
    put_cmd = models.Toggle('CMD:put', zname='Put', desc='Put Pin')
    get_cmd = models.Toggle('CMD:get', zname='Get', desc='Get Pin')
    getput_cmd = models.Toggle('CMD:getPut', zname='GetP ut', desc='Get Put Pin')
    pause_cmd = models.Toggle('CMD:pause', zname='Pause', desc='Pause')
    tool_cmd = models.Toggle('CMD:setTool', zname='Set Tool', desc='Set Tool')
    calib_cmd = models.Toggle('CMD:toolCal', zname='Tool Cal', desc='Cal Tool')
    back_cmd = models.Toggle('CMD:back', zname='Back', desc='Back')
    clear_cmd = models.Toggle('CMD:clear', zname='Clear', desc='Clear')
    abort_cmd = models.Toggle('CMD:abort', zname='Abort', desc='Abort')
    set_cmd = models.Toggle('CMD:setSample', zname='Set Sample', desc='Set Sample')
    home_cmd = models.Toggle('CMD:home', zname='Home', desc='Home')

    # Plate commands
    put_plate_cmd = models.Toggle('CMD:putPlate', desc='Put Plate')
    get_plate_cmd = models.Toggle('CMD:getPlate', desc='Get Plate')
    getput_plate_cmd = models.Toggle('CMD:getPutPlate', desc='Get Put Plate')
    adjust_cmd = models.Toggle('CMD:adjPlate', desc='Adjust Plate')
    tilt_cmd = models.Toggle('CMD:tiltPlate', desc='Tilt Plate')
    expose_cmd = models.Toggle('CMD:expose', desc='Expose')
    collect_cmd = models.Toggle('CMD:collect', desc='Collect')
    restart_cmd = models.Toggle('CMD:restart', desc='Restart')

    # Simplified commands
    dismount_cmd = models.Toggle('CMD:dismount', desc='Dismount')
    mount_cmd = models.Toggle('CMD:mount', desc='Mount')


def port2args(port):
    # converts 'L1C1' to lid=1, sample=21, tool=2 for SPINE puck where NUM_PUCK_SAMPLES = 10
    # converts 'P2B1' to plate=2, well=24, tool=3 for Plates puck where NUM_ROW_WELLS = 24
    if len(port) < 4: return {}
    args = {}
    if port.startswith('L'):
        args = {
            'tool': ToolType.PUCK.value,
            'lid': zero_int(port[1]),
            'sample' : 'ABC'.index(port[2])*NUM_PUCK_SAMPLES + zero_int(port[3:]),
            'mode': 'puck'
        }
    elif port.startswith('P'):
        args = {
            'tool': ToolType.PLATE.value,
            'plate': zero_int(port[1]),
            'well': 'ABCDEFGH'.index(port[2])*NUM_ROW_WELLS + zero_int(port[3:]),
            'mode': 'plate'
        }
    return args


def pin2port(lid, sample):
    # converts lid=1, sample=21 to 'L1C1'
    puck, pin = divmod(sample, NUM_PUCK_SAMPLES)
    if all((lid, pin)):
        return 'L{}{}{}'.format(lid, 'ABC'[puck], pin)
    else:
        return ''


def plate2port(plate, well):
    # converts plate=1, well=21 to 'P1A21'
    row, sample = divmod(well, NUM_ROW_WELLS)
    if all((plate, sample)):
        return 'P{}{}{}'.format(plate, 'ABCDEFGH'[row], sample)
    else:
        return ''


def zero_int(text):
    try:
        return int(text)
    except ValueError:
        return 0


class BobCATSApp(object):
    def __init__(self, device_name, address, command_port=1000, status_port=10000):
        self.ioc = BobCATS(device_name, callbacks=self)
        self.inbox = Queue()
        self.outbox = Queue()
        self.send_on = False
        self.recv_on = False
        self.user_enabled = False
        self.ready = False
        self.command_client = cats.CommandFactory(self)
        self.status_client = cats.StatusFactory(self)
        self.pending_clients = {self.command_client.protocol.message_type, self.status_client.protocol.message_type}

        reactor.connectTCP(address, status_port, self.status_client)
        reactor.connectTCP(address, command_port, self.command_client)

        # status pvs and conversion types
        self.status_map = [
            (self.ioc.power_fbk, int), (self.ioc.mode_fbk, int), (self.ioc.default_fbk, int),
            (self.ioc.tool_fbk, zero_int), (self.ioc.path_fbk, str), (self.ioc.lid_tool_fbk, zero_int),
            (self.ioc.sample_tool_fbk, zero_int), (self.ioc.sample_diff_fbk, zero_int), (self.ioc.lid_diff_fbk, zero_int),
            (self.ioc.sample_diff_fbk, zero_int), (self.ioc.plate_fbk, zero_int), (self.ioc.well_fbk, zero_int),
            (self.ioc.barcode_fbk, str), (self.ioc.running_fbk, int), (self.ioc.ln2_dew1_fbk, int),
            (self.ioc.ln2_dew2_fbk, int), (self.ioc.speed_fbk, int), (self.ioc.pucks_dew1_fbk, str),
            (self.ioc.pucks_dew2_fbk, str), (self.ioc.pos_dew1_fbk, zero_int), (self.ioc.pos_dew2_fbk, zero_int)
        ]

    def ready_for_commands(self):
        return self.ready and self.ioc.enabled.get() and self.ioc.connected.get()

    def sender(self):
        self.send_on = True
        epics.threads_init()
        while self.send_on:
            command = self.outbox.get()
            logger.debug('< {}'.format(command))
            try:
                self.command_client.send_message(command)
            except Exception as e:
                logger.error(e)
            time.sleep(0)

    def receiver(self):
        self.recv_on = True
        epics.threads_init()
        while self.recv_on:
            message, message_type = self.inbox.get()
            logger.debug('> {}'.format(message))
            try:
                self.process_message(message, message_type)
            except Exception as e:
                logger.error(e)
            time.sleep(0)

    def status_monitor(self):
        epics.threads_init()
        self.recv_on = True
        commands = ['state', 'di', 'do', 'position']
        cmd_index = 0
        while self.recv_on:
            self.status_client.send_message(commands[cmd_index])
            cmd_index = (cmd_index + 1) % len(commands)
            time.sleep(STATUS_TIME)

    def disconnect(self, client_type):
        self.pending_clients.add(client_type)
        self.recv_on = False
        self.send_on = False
        self.ioc.connected.put(0)

    def connect(self, client_type):
        self.pending_clients.remove(client_type)

        # all clients connected
        if not self.pending_clients:
            self.inbox.queue.clear()
            self.outbox.queue.clear()
            send_thread = Thread(target=self.sender)
            recv_thread = Thread(target=self.receiver)
            status_thread = Thread(target=self.status_monitor)
            send_thread.setDaemon(True)
            recv_thread.setDaemon(True)
            status_thread.setDaemon(True)
            send_thread.start()
            recv_thread.start()
            status_thread.start()
            self.ready = True
            self.ioc.connected.put(1)
            logger.warn('Controller ready!')
        else:
            self.ready = False

    def shutdown(self):
        logger.warn('Shutting down ...')
        self.recv_on = False
        self.send_on = False
        self.ioc.shutdown()

    def send_command(self, command, *args):
        if self.ready_for_commands():
            if args:
                cmd = '{}({})'.format(command, ','.join([str(arg) for arg in args]))
            else:
                cmd = command
            self.outbox.put(cmd)

    def receive_message(self, message, message_type):
        self.inbox.put((message, message_type))

    def process_message(self, message, message_type):
        if message_type == cats.MessageType.STATUS:
            # process state messages
            self.parse_status(message)
        else:
            # process response messages
            self.ioc.log.put(message)

    def parse_status(self, message):
        patt = re.compile('^(?P<context>\w+)\((?P<msg>.*?)\)')
        m = patt.match(message)
        if m:
            details = m.groupdict()
            if details['context'] == 'state':
                for i, value in enumerate(details['msg'].split(',')):
                    variable, converter = self.status_map[i]
                    try:
                        variable.put(converter(value))
                    except ValueError:
                        logger.warning('Unable to parse state: {}'.format(message))
                if self.ioc.mode_fbk.get() == 1 and self.ioc.default_fbk.get() == 1:
                    if self.ioc.running_fbk.get():
                        self.ioc.status.put(StatusType.BUSY.value)
                    else:
                        self.ioc.status.put(StatusType.IDLE.value)
                else:
                    self.ioc.status.put(StatusType.ERROR.value)
            elif details['context'] == 'do':
                self.ioc.outputs_fbk.put(int(details['msg'].replace(',', ''), 2))
            elif details['context'] == 'di':
                self.ioc.inputs_fbk.put(int(details['msg'].replace(',', ''), 2))

    # callbacks
    def do_mount_cmd(self, pv, value, ioc):
        if value:
            port = ioc.next_param.get().strip()
            current = ioc.mounted_fbk.get().strip()
            params = port2args(port)
            plate_type = ioc.plate_type.get()
            if all(params.values()):
                if params['mode'] == 'puck':
                    command = 'put' if not current else 'getput'
                    args = (params['tool'], params['lid'], params['sample']) + 10 * (0,)
                elif params['mode'] == 'plate':
                    ontool = ioc.tooled_fbk.get().strip()
                    command = 'putplate' if not ontool else 'getputplate'
                    args = (params['tool'],) + 4*(0, )+(params['plate'], params['well'], plate_type)
                else:
                    return
                self.send_command(command, *args)

    def do_dismount_cmd(self, pv, value, ioc):
        if value:
            current = ioc.mounted_fbk.get().strip()
            params = port2args(current)
            if all(params.values()):
                if params['mode'] == 'puck':
                    command = 'get'
                elif params['mode'] == 'plate':
                    command = 'getplate'
                else:
                    return
                self.send_command(command, params['tool'])

    def do_sample_diff_fbk(self, pv, value, ioc):
        port = pin2port(ioc.lid_diff_fbk.get(), value)
        ioc.mounted_fbk.put(port)

    def do_sample_tool_fbk(self, pv, value, ioc):
        port = pin2port(ioc.lid_tool_fbk.get(), value)
        ioc.tooled_fbk.put(port)

    def do_well_fbk(self, pv, value, ioc):
        port = plate2port(ioc.plate_fbk.get(), value)
        ioc.tooled_fbk.put(port)

    def do_lid_cmd(self, pv, value, ioc):
        lid = ioc.lid_param.get()
        if lid:
            action = 'open' if value == 1 else 'close'
            self.send_command('{}lid{}'.format(action, lid))
        else:
            ioc.warning.put('Please select a lid first!')

    def do_put_cmd(self, pv, value, ioc):
        lid = ioc.lid_param.get()
        sample = ioc.sample_param.get()
        tool = ioc.tool_param.get()
        if value and lid and sample and tool == ToolType.PUCK.value:
            args = (tool, lid, sample) + 10*(0, )
            self.send_command('put', *args)

    def do_get_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool == ToolType.PUCK.value:
            self.send_command('get', tool)

    def do_getput_cmd(self, pv, value, ioc):
        lid = ioc.lid_param.get()
        sample = ioc.sample_param.get()
        tool = ioc.tool_param.get()
        if value and lid and sample and tool == ToolType.PUCK.value:
            args = (tool, lid, sample) + 10*(0, )
            self.send_command('getput', *args)

    def do_pause_cmd(self, pv, value, ioc):
        if value:
            self.send_command('pause')

    def do_tool_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool:
            self.send_command('home', tool)

    def do_home_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool:
            self.send_command('home', tool)

    def do_calib_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool:
            self.send_command('toolcal', tool)

    def do_back_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool:
            self.send_command('back', tool)

    def do_clear_cmd(self, pv, value, ioc):
        if value :
            self.send_command('clear memory')

    def do_abort_cmd(self, pv, value, ioc):
        if value :
            self.send_command('abort')

    def do_restart_cmd(self, pv, value, ioc):
        if value :
            self.send_command('restart')

    def do_power_cmd(self, pv, value, ioc):
        if value :
            self.send_command('reset')
            reactor.callLater(1, self.send_command, 'on')
        else:
            self.send_command('off')

    def do_set_cmd(self, pv, value, ioc):
        lid = ioc.lid_param.get()
        sample = ioc.sample_param.get()
        tool = ioc.tool_param.get()
        if value and lid and sample and tool:
            self.send_command('setdiffr', lid, sample, tool)

    def do_put_plate_cmd(self, pv, value, ioc):
        plate = ioc.plate_param.get()
        plate_type = ioc.plate_type.get()
        well = ioc.well_param.get()
        tool = ioc.tool_param.get()
        if ioc.plates_enabled.get() and value and plate and plate_type and tool == ToolType.PLATE.value:
            args = (tool,) + 4*(0, )+(plate, well, plate_type)
            self.send_command('putplate', *args)

    def do_get_plate_cmd(self, pv, value, ioc):
        tool = ioc.tool_param.get()
        if value and tool == ToolType.PLATE.value:
            self.send_command('getplate', tool)

    def do_getput_plate_cmd(self, pv, value, ioc):
        plate = ioc.plate_param.get()
        plate_type = ioc.plate_type.get()
        well = ioc.well_param.get()
        tool = ioc.tool_param.get()
        drop = ioc.plate_drop.get()
        if ioc.plates_enabled.get() and value and plate and plate_type and tool == ToolType.PLATE.value:
            args =  (tool,) + 4*(0, )+(plate, well, plate_type,drop)
            self.send_command('getputplate', *args)

    def do_adjust_cmd(self, pv, value, ioc):
        x = ioc.adjust_x.get()
        y = ioc.adjust_y.get()
        tool = ioc.tool_param.get()
        if value and ioc.plates_enabled.get() and tool == ToolType.PLATE.value:
            args =  (tool,) + 9*(0, )+(x, y)
            self.send_command('adjust', *args)

    def do_tilt_cmd(self, pv, value, ioc):
        ang = ioc.plate_angle.get()
        tool = ioc.tool_param.get()
        if value and ioc.plates_enabled.get() and tool == ToolType.PLATE.value:
            args = (tool,) + 12 * (0,) + (ang,)
            self.send_command('plateangle', *args)

    def do_focus_cmd(self, pv, value, ioc):
        z = ioc.adjust_z.get()
        tool = ioc.tool_param.get()
        if value and ioc.plates_enabled.get() and tool == ToolType.PLATE.value:
            args = (tool,) + 11 * (0,) + (z,)
            self.send_command('focus', *args)

    def do_expose_cmd(self, pv, value, ioc):
        start = ioc.start_angle.get()
        delta = ioc.delta_angle.get()
        exposure = ioc.exposure.get()
        steps = ioc.steps_param.get()
        end_angle = ioc.end_angle.get()
        tool = ioc.tool_param.get()

        if value and ioc.plates_enabled.get() and tool == ToolType.PLATE.value:
            args = (tool,) + 12 * (0,) + (start, delta, exposure, steps)
            self.send_command('expose', *args)

    def do_collect_cmd(self, pv, value, ioc):
        start = ioc.start_angle.get()
        delta = ioc.delta_angle.get()
        exposure = ioc.exposure.get()
        steps = ioc.steps_param.get()
        end_angle = ioc.end_angle.get()
        tool = ioc.tool_param.get()

        if value and ioc.plates_enabled.get() and tool == ToolType.PLATE.value:
            args = (tool,) + 12 * (0,) + (start, delta, exposure, steps, end_angle)
            self.send_command('collect', *args)
