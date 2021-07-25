"""RESTful HTTP API for controlling a Raspberry Pi thermostat. API endpoints define setpoints for 8 3hr time intervals
throughout a 24hr day: 0-3, 3-6, 6-9, etc. Additionally, the user may override the scheduled setpoint for the next 3 hours.
Includes built-in hysteresis to avoid rapid on-off switching of HVAC systems; this hysteresis is not exposed in the API
for safety reasons.
"""
import collections
import datetime
import conf

# To help avoid confusion import statements should be organized as:
# standard library,
# 3rd party dependencies,
# then local imports

import flask
import flask.json
from flask import request
import logging
import time
import os
import rpi_relay  # local
import state   # local
import Queue  # followed by standard library
import werkzeug.exceptions  # followed by 3rd party

from apscheduler.schedulers.background import BackgroundScheduler

app = flask.Flask(__name__)


# Temperature setpoint is determined by the time of day, stored in SETPOINT_DB.
TEMP_SETPOINT_HOURS = (0, 3, 6, 9, 12, 15, 18, 21)

def get_request_db():
    "Returns a dbm database. Use only in a Flask app context!"
    db = getattr(flask.g, '_database', None)
    if db is None:
        # open a new connection as needed -- throughput doesn't need to be high for this!
        db = flask.g._database = state.get_conn()
    # We are assuming here that db has successfully received a connection
    #  methods that depend on this connection will fail unpredictably if `db`
    #  is still None
    return db

@app.teardown_appcontext
def close_connection(exception):
    # We should check the exceptions and log / report them
    #  or register and errorhandler()
    db = getattr(flask.g, '_database', None)
    if db is not None:
        db.close()

def to_farenheit(c):
    return 9.0/5.0 * c + 32

def get_setpoint(hour, db=None):
    "Returns the temp setpoint for the given hour of day"
    # This condition is redundant,
    #  we should remove the default and call get_request_db() prior to calling
    #  get_setpoint()
    if db is None:
        db = get_request_db()
    # Pep line length, won't mention again
    setpoint_key = [set_hr for set_hr in TEMP_SETPOINT_HOURS if hour >= set_hr][-1]
    # Could avoid passing the db dependency around since it is not used to retrieve the key
    #  then could rename this method to get_setpoint_key(), or since it is used once,
    #  we could reduce indirection by bringing line 65 back to `bangbang_controller()`
    return db[setpoint_key]

def parse_setpoints(json_form):
    # Method will be easier / cleaner to test if we remove the flask dependency
    #  by loading the json before calling
    form = flask.json.loads(json_form['setpoints'])
    setpoints = {}

    for setpoint, val in form.iteritems():
        if isinstance(setpoint, basestring):
            setpoint = int(setpoint)
        if isinstance(val, basestring):
            val = float(val)
        if setpoint in TEMP_SETPOINT_HOURS:
            setpoints[setpoint] = val
        else:
            raise Exception("setpoint %s not valid" % setpoint)
    return setpoints


# The following endpoints would all benefit from some Sphinx or pdoc autodocs

@app.route('/api/v1/setpoints/', methods=('POST', 'GET'))
def handle_setpoints_request():
    db = get_request_db()
    if request.method == 'POST':
        setpoints = parse_setpoints(request.form)
        for hr, temp in setpoints.iteritems():
            db[hr] = temp
        return flask.json.jsonify(setpoints)

    if request.method == 'GET':
        setpoints = {hr: db.get(hr) for hr in TEMP_SETPOINT_HOURS}
        return flask.json.jsonify(setpoints)

@app.route('/api/v1/status/', methods=('GET',))
def return_relay_status():
    return flask.json.jsonify({'ac_on': rpi_relay.ac_status()})

@app.route('/api/v1/mode/', methods=('GET', 'POST'))
def handle_thermostat_mode():
    if request.method == 'GET':
        return flask.json.jsonify({'mode': state.CURRENT_MODE})

    if request.method == 'POST':
        mode = request.form.get('mode')
        # Do we want HTML errors or should we return JSON errors?
        assert mode in [state.ThermostatModes.AUTO, state.ThermostatModes.MANUAL, state.ThermostatModes.OFF]
        state.CURRENT_MODE = mode
        return flask.json.jsonify({'mode': state.CURRENT_MODE})

@app.route('/api/v1/temperature/', methods=('POST', 'GET'))
def handle_temp():
    logger.info('in temperature')
    if request.method == 'POST':
        logger.warn(request.form)
        temp = float(request.form.get('temperature'))
        if conf.FARENHEIT is True:
            temp = to_farenheit(temp)
        humidity = float(request.form.get('humidity'))
        logger.warn('temp=%s, humidity=%s' % (temp, humidity))
        now = time.time()
        state.TEMPERATURE_READINGS.append((now, temp))
        state.HUMIDITY_READINGS.append((now, humidity))
        return 'ok'  # inconsistent with other json return values
    if request.method == 'GET':
        temperatures = [x for x in state.TEMPERATURE_READINGS]
        humidities = [x for x in state.HUMIDITY_READINGS]
        return flask.json.jsonify(dict(temperature=temperatures, humidity=humidities))

@app.route('/api/v1/timer/', methods=('POST', 'GET'))
def handle_timer_request():
    """manual override for turning the AC on for a set amount of time."""
    def get_manual_status():
        if state.EVENT_QUEUE.queue:
            now = time.time()
            future_events = filter(lambda x: x[0] > now, state.EVENT_QUEUE.queue)
            if future_events:
                future_e, status = future_events[0]
                return flask.json.jsonify(dict(future_sec=(future_e - now), future_status=status))

        return flask.json.jsonify({})

    def handle_timer(on_time):
        if (on_time < conf.MIN_ON_TIME) or (on_time > conf.MAX_ON_TIME):
            raise werkzeug.exceptions.BadRequest(description='time_on exceeds valid params')
        turn_off_event = (time.time() + on_time, False)
        turn_on_event = (time.time(), True)
        new_queue = Queue.PriorityQueue()
        new_queue.put(turn_on_event)
        new_queue.put(turn_off_event)
        state.EVENT_QUEUE = new_queue

    if request.method == 'POST':
        on_time_int = int(request.form['on_time'])
        handle_timer(on_time_int)
        return get_manual_status()

    if request.method == 'GET':
        return get_manual_status()

def event_handler():
    logger = logging.getLogger('task_queue')
    q = state.EVENT_QUEUE
    conn = state.get_conn()
    try:
        exec_time, event = q.get(block=False)
        now = time.time()
        if now > exec_time:
            rpi_relay.set_ac_relay(event, conn)
            logger.info("setting relay=%s" % event)
        else:
            # put the event back into the queue if it isn't time to execute it yet
            q.put((exec_time, event))
        q.task_done()
    except Queue.Empty:
        pass

def bangbang_controller():
    def is_stale(timestamp):
        if time.time() - int(timestamp) > state.STALE_READ_INTERVAL:
            return True
        return False

    logger = logging.getLogger('bangbang_controller')

    if state.CURRENT_MODE != state.ThermostatModes.AUTO:
        logger.warn("mode is set to %s" % state.CURRENT_MODE)
        return


    conn = state.get_conn()  # should use get_request_db()
    temp_read_time, most_recent_temp = state.TEMPERATURE_READINGS[-1]
    humid_read_time, most_recent_humidity = state.HUMIDITY_READINGS[-1]

    if is_stale(temp_read_time) or is_stale(humid_read_time):
        state.CURRENT_MODE = state.ThermostatModes.MANUAL
        logger.error("temperature readings are stale! setting mode to MANUAL")
        return

    now = datetime.datetime.now()
    current_setpoint = get_setpoint(now.hour, db=conn)  # don't pass db, keep it here, use it before method

    # current_setpoint could be None
    if (most_recent_temp - current_setpoint) > (conf.HYSTERESIS_TEMP / 2.0):
        turn_on_event = (time.time(), True)
        state.EVENT_QUEUE.put(turn_on_event)
        if rpi_relay.ac_status() is False:
            logger.warn('Temp=%s, setpoint=%s, Setting AC ON' % (most_recent_temp, current_setpoint))
    elif (current_setpoint - most_recent_temp) > (conf.HYSTERESIS_TEMP / 2.0):
        turn_off_event = (time.time(), False)
        state.EVENT_QUEUE.put(turn_off_event)
        if rpi_relay.ac_status() is True:
            logger.warn('Temp=%s, setpoint=%s, Setting AC OFF' % (most_recent_temp, current_setpoint))

@app.route('/<path:path>/')
def resources(path):
    return flask.send_from_directory(STATIC_DIR, path)

@app.route('/')
def index():
    # Use the STATIC_DIR global instead of hard coded string
    return flask.send_file('static/index.html')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(asctime)s %(message)s')
    logger = logging.getLogger('main')

    STATIC_DIR = os.environ.get('STATIC_DIR', 'static')
    rpi_relay.init_RPi()
    scheduler = BackgroundScheduler()
    scheduler.start()

    scheduler.add_job(event_handler, 'interval', seconds=conf.EVENT_LOOP_INTERVAL)
    scheduler.add_job(bangbang_controller, 'interval', seconds=conf.BANGBANG_LOOP_INTERVAL)
    logger.warn('starting scheduler')
    logger.warn('starting web server')
    app.run(debug=False, host='0.0.0.0')
