#!/usr/bin/env python3

# Game Plan
# Python Programm runs for 1h, then scheduled again for every 1h during 6am and 22pm by cron. 
# Pkill and pkill-9  by cron before each start. This is for robustness. 
# Alternative: systemd unit

# Note on the Huawei Sun2000 inverter
# Initially, I had V100R001C00SPC125 installed on the SDongleA-05, which had very unstable
# modbus TCP connections. Got several minutes of not being able to connect during the day. 
# Upgrading the Dongle to V100R001C00SPC133 made this only slightly better. The issue still exists. 
# Apparently, this can only be fixed by connecting to the internal Wifi AP of the inverter
# see: https://skyboo.net/2022/02/huawei-sun2000-why-using-a-usb-dongle-for-monitoring-is-not-a-good-idea/
# 
# Update: updating the dongle from V148 to V152 changed things for the better. Getting more stable
# responses now, even though sometimes still connection errors happen. The dongle seems to like to reboot once a day or so. 
# For now, to me the winning combination seems to be V133 for SDongleA-05 and V152 for the SUN2000-10KTL-M1. 

# Update2: https://gitlab.com/Emilv2/huawei-solar has the Huawei magic bytes added to the modbus communication 
# (apparently from reversing the modbus communication between SDongle, Meter and Battery). It also has a few tricks 
# added such as a heart-beat write to a magic modbus register (49999) every 15 secs to avoid closing the connection. 
# Maybe use this instead of our own modbus implementation?


from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException, ConnectionException
from datetime import datetime
from dataclasses import dataclass
import time
import math
import typing
import requests

import logging
logging.basicConfig(level = logging.INFO)


class ScheduleableDevice:
    """Device with known power consumption that can be scheduled (turned on and off at will)"""
    name: str
    wattage: int
    times_wattage_exceeded: int = 0
    state: str

    def __init__(self, name, wattage):
        self.name = name
        self.wattage = wattage
        self.state = 'unknown'


class ShellyRelayDevice(ScheduleableDevice):
    """Shelly Relay modelled as scheduleable device"""
    ip: str

    def __init__(self, name, wattage, ip):
        super().__init__(name, wattage)
        self.ip = ip
        self.initializeState()

    def turnOn(self):
        self.state = self.turnRelais('on')

    def turnOff(self):
        self.state = self.turnRelais('off')

    def initializeState(self):
        response = requests.post("http://{}/relay/0".format(self.ip), timeout = 5)
        self.state = self.__parseShellyState(response)

    def turnRelais(self, on_off: str):
        # manual test
        # $ curl -s -X POST "192.168.1.84/relay/0?turn=on" -d ""
        # $ {"ison":true,"has_timer":false,"timer_started":0,"timer_duration":0,"timer_remaining":0,"source":"http"}
        response = requests.post("http://{}/relay/0?turn={}".format(self.ip, on_off), timeout = 5)
        return self.__parseShellyState(response)

    def __parseShellyState(self, response):
        logging.info("Calling shelly returned: %s", response)
        logging.info("Response text: %s", response.text)

        state = 'unknown'
        if response.status_code == 200:
            res = response.json().get('ison')
            logging.info("Relais isON after parsing state response: %s", res)
            if res == True: state = 'on'
            else: state = 'off'
        else:
            logging.error("Shelly HTTP code: %s", response.status_code)
        return state


# TODO
# Model go-e charger as many on/off devices 500W apart. Turning on means power change + 500W. Turning off means power change -500W
# https://github.com/cathiele/goecharger/
# https://go-e.co/app/api.pdf
# https://www.goingelectric.de/forum/viewtopic.php?p=1797041&sid=f1cbeedd532aecfc261755320adbb3ee#p1797041
# https://github.com/goecharger/go-eCharger-API-v2/blob/main/http-de.md

# TODO
# Model AIP heatpump as on/off device and decide if we want to use the luxtronic python API or just
# flip a shelly switch connected to the SGReady input wires. 
# https://github.com/Bouni/luxtronik, 
# https://github.com/Bouni/python-luxtronik/ 
# https://wiki.fhem.de/wiki/Luxtronik_2.0
# https://www.haustechnikdialog.de/Forum/t/229858/Alpha-Innotec-WZSV-SG-Ready-funktioniert-nicht


class Sun2000Client:
    """Talk to Sun2000 inverter using modbus tcp"""
    # based on https://github.com/olivergregorius/sun2000_modbus
    # and https://javierin.com/wp-content/uploads/sites/2/2021/09/Solar-Inverter-Modbus-Interface-Definitions.pdf

    host:str
    slave:int
    client = None

    def __init__(self, host, slave):
        self.host = host
        self.slave = slave
        self.client = ModbusTcpClient(host=host, port=502, timeout=15, reconnect_delay=3, retry_on_empty=True)

    def connect(self):
        if not self.isConnected(): 
            logging.info("Connecting to inverter...")
            self.client.connect()

            if self.isConnected():
                logging.info("sleeping")
                time.sleep(3) # required, wait at least 2secs after connect
                logging.info('Successfully connected to inverter')
                return True
            else:
                logging.error('Inverter connection failed!')  
                return False

    def isConnected(self):
        return self.client.is_socket_open()
    
    def disconnect(self):
        logging.info("disconnecting from inverter...")
        self.client.close()
        logging.info("done.")
    
    
    ### data reading part ###

    @dataclass
    class MeterValues:
        MeterStatus: str = 'n/a'
        ActivePower: int = 0

    def readPowerMeter(self): 
        #MeterStatus = Register(37100, 1, datatypes.DataType.UINT16_BE, 1, None, AccessType.RO, mappings.MeterStatus)
        #ActivePower = Register(37113, 2, datatypes.DataType.INT32_BE, 1, "W", AccessType.RO, None)

        #this reads 15 2-byte values (each register is 2 bytes) = 30 bytes and adds 1 len byte at front. result is 31 bytes
        #we are interested in the first 2 bytes and the last 4 bytes of the reponse (registers 37100 and 37113)
        result = self.client.read_holding_registers(37100, 15, slave=self.slave)  
        if type(result) == ModbusIOException: raise result
        
        MeterStatus = {
            0: 'offline',
            1: 'online'
        }

        return Sun2000Client.MeterValues(
            MeterStatus.get(self.__decode_uint_be(result.encode()[1:3])), # MeterStatus
            self.__decode_int_be(result.encode()[27:31]) #ActivePower
        )
   
    def readBattery(self): 
        #RunningStatus = Register(37762, 1, datatypes.DataType.UINT16_BE, 1, None, AccessType.RO, mappings.RunningStatus)
        #ChargeDischargePower = Register(37765, 2, datatypes.DataType.INT32_BE, 1, "W", AccessType.RO, None)
        
        #this reads 5 2-byte values (each register is 2 bytes) = 10 bytes and adds 1 len byte at front. result is 11 bytes
        #we are interested in the first 2 bytes and the last 4 bytes of the reponse (registers 37762 and 37765)
        result = self.client.read_holding_registers(37762, 5, slave=self.slave)  
        if type(result) == ModbusIOException: raise result
       
        RunningStatus = {
            0: 'offline',
            1: 'standby',
            2: 'running',
            3: 'fault',
            4: 'sleep mode'
        }

        return Sun2000Client.MeterValues(
            RunningStatus.get(self.__decode_uint_be(result.encode()[1:3])), # RunningStatus
            self.__decode_int_be(result.encode()[7:11]) #ChargeDischargePower
        )

    def __decode_string(self, value):
        return value.decode("utf-8", "replace").strip("\0")

    def __decode_uint_be(self, value):
        return int.from_bytes(value, byteorder="big", signed=False)

    def __decode_int_be(self, value):
        return int.from_bytes(value, byteorder="big", signed=True)

    def __decode_bitfield(self, value):
        return ''.join(format(byte, '08b') for byte in value)


class ExcessPowerScheduler:
    """Check limits in order, once first limit exceeded inform caller and reset counters before next is checked"""
    devices = None
    times_power_negative = 0

    POSITIVE_POWER_SAFETY_MARGIN = 100 #watts
    NEGATIVE_POWER_SAFETY_MARGIN = -20 #watts
    POWER_ON_HYSTERESIS = 4 #num times limit exceeded consequtively as power ON condition
    POWER_OFF_HYSTERESIS = 2 #num times limit exceeded consequtively as power OFF condition

    def __init__(self, devices: typing.List[ScheduleableDevice]):
        self.devices = devices

    def schedule(self, power):
        logging.info("## %s: New scheduler cycle. power=%s, times_negative=%s", datetime.now(), power, self.times_power_negative)

        if power == None: return  # error reading house power. ignore.

        if power > self.POSITIVE_POWER_SAFETY_MARGIN:
            ## ok, we have excess power!
            for dev in self.devices:
                logging.info("Checking if we can turn ON device: %s with state: %s and limit_counter: %s", 
                    dev.name, dev.state, dev.times_wattage_exceeded)
                if dev.state == 'off': 
                    if (power + self.POSITIVE_POWER_SAFETY_MARGIN) > dev.wattage: 
                        dev.times_wattage_exceeded += 1
                    
                    if dev.times_wattage_exceeded >= self.POWER_ON_HYSTERESIS: 
                        logging.info("Turning ON device: " + dev.name)
                        dev.turnOn()
                        self.__resetAllDeviceCounters()
                        break # turn on devices one at a time, in order given
        
        elif power < self.NEGATIVE_POWER_SAFETY_MARGIN:
            ## oh oh, too much scheduled. we import from grid now. drop loads!
            self.times_power_negative += 1
            if self.times_power_negative >= self.POWER_OFF_HYSTERESIS:
                for dev in reversed(self.devices): # traverse in reverse order
                    logging.info("Checking if we can turn OFF device: %s with state: %s and limit_counter: %s", 
                        dev.name, dev.state, dev.times_wattage_exceeded)
                    if dev.state == 'on':
                        logging.info("Turning OFF device: " + dev.name)
                        dev.turnOff()
                        break # turn off devices one at a time, in reverse order
                self.times_power_negative = 0
        
        else:
            self.times_power_negative = 0 # reset negative power count

    def __resetAllDeviceCounters(self):
        for dev in self.devices:
            dev.times_wattage_exceeded = 0


class PowerScheduler: 
    inverter = None
    scheduler = ExcessPowerScheduler([
            ## list of excess power consumers to be scheduled, in priority order (highest prio first)
            #ShellyRelayDevice('heating_low', 800, '192.168.1.84'), 
            #ShellyRelayDevice('heating_high', 1800, '192.168.1.84')
        ])

    def __init__(self):
        self.inverter = Sun2000Client(host = '192.168.1.51', slave=1)

        # exit handler
        import atexit
        atexit.register(self.inverter.disconnect)
        import signal
        signal.signal(signal.SIGTERM, self.handle_exit)
        signal.signal(signal.SIGINT, self.handle_exit)

    def handle_exit(self, signum, frame):
        import sys
        sys.exit(0)
    
    def readHouseActivePower(self):
        excessPower = None

        try:
            self.__ensureConnection()

            meter = self.inverter.readPowerMeter()
            battery = self.inverter.readBattery()

            gridPower = None
            logging.info("Meter status: %s", meter.MeterStatus)
            if meter.MeterStatus == 'online':
                gridPower = meter.ActivePower
                logging.info("Meter power (+feed-to-grid/-use-from-grid): %s", gridPower)

            batteryPower = None
            logging.info("Battery status: %s", battery.MeterStatus)
            if battery.MeterStatus == 'running':
                batteryPower = battery.ActivePower
                logging.info("Battery power (+charge/-discharge): %s", batteryPower)

            if gridPower!= None and batteryPower != None: 
                if batteryPower < 0 and gridPower <= 0:    # battery discharging and importing from grid.
                    excessPower = batteryPower + gridPower  # --> return negative sum of all consumers. 
                elif batteryPower >= 0 and gridPower <= 0:  # battery charging, but importing from grid.
                    excessPower = gridPower - batteryPower  # --> return negative sum. the moment we are charging battery we dont want to schedule anything.
                elif batteryPower < 0 and gridPower >= 0:  # battery discharing, and feeding to grid.
                    excessPower = batteryPower - gridPower  # --> return negative sum: any excess power while battery is discharging is coming from battery
                elif batteryPower >= 0 and gridPower >= 0:  # battery charging, feeding to grid.
                    excessPower = gridPower #+batteryPower  # --> we only want to schedule the excess of the grid feed, always prefer to charge the battery.
                else: # impossible
                    excessPower = gridPower
        except (ModbusIOException, ConnectionException) as ex:
            logging.exception(ex) # print and continue
        
        logging.info("Excess power: %s", excessPower)
        return excessPower

    def __ensureConnection(self):
        if not self.inverter.isConnected():
            for i in range (2 * 10): #10min
                if self.inverter.connect(): break
                else: time.sleep(30)

    def runFiniteSchedulerLoop(self): 
        for x in range(2 * 60): #60min
            start = time.time()
            power = self.readHouseActivePower()
            elapsed = time.time() - start

            logging.info("Seconds taken to read values: %.2fs" % elapsed)
            if elapsed > 100: logging.warning("!!!! EXCESSIVE MODBUS TIME !!!!")

            self.scheduler.schedule(power) 

            time.sleep(30)
        self.inverter.disconnect()

## main
if __name__ == '__main__':
    logging.info("### Starting Excess Power Scheduler ###")

    sched = PowerScheduler()

    sched.runFiniteSchedulerLoop()
    #sched.scheduler.schedule(2000.0)

    #sched.scheduler.schedule(-2000.0)
    #sched.scheduler.schedule(-2000.0)
    #sched.scheduler.schedule(-2000.0)

