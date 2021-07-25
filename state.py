import collections
import sqlite3dbm
import Queue
import conf

class ThermostatModes():
    AUTO = 'auto'
    MANUAL = 'manual'
    OFF = 'off'

# Unused, delete
def get_ro_conn():
    return sqlite3dbm.sshelve.open(conf.SETPOINT_DB, 'r')

def get_conn():
    return sqlite3dbm.sshelve.open(conf.SETPOINT_DB)

STALE_READ_INTERVAL = 5 * 60 # in seconds

EVENT_QUEUE = Queue.PriorityQueue()
TEMPERATURE_READINGS = collections.deque(maxlen=1 * 24 * 60)
HUMIDITY_READINGS = collections.deque(maxlen=1 * 24 * 60)
# accesses the unix epoch for when the AC relay was last switched OFF
MOST_RECENT_OFF_KEY = 'most_recent_off'

# accesses the unix epoch for when the AC relay was last switched ON
MOST_RECENT_ON_KEY = 'most_recent_on'

# Mode is initialized to AUTO.
CURRENT_MODE = ThermostatModes.AUTO