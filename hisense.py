#!/usr/bin/env python3.7
"""
Server for controlling HiSense Air Conditioner WiFi modules.
These modules are embedded for example in the Israel Tornado ACs.
This module is based on reverse engineering of the AC protocol,
and is not affiliated with HiSense, Tornado or any other relevant
company.

In order to run this server, you need to provide it with the a
config file, that likes like this:
{"lanip_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
 "lanip_key_id":8888,
 "random_1":"YYYYYYYYYYYYYYYY",
 "time_1":201111111111111,
 "random_2":"XXXXXXXXXXXXXXXX",
 "time_2":111111111111}

The random/time values are regenerated on key exchange when the
server first starts talking with the AC, so is the lanip_key_id.
The lanip_key, on the other hand, is generated only on the
HiSense server. In order to get that value, you'll need to either
sniff the TLS-encrypted network traffic, or fetch and unencrypt
the string locally stored by the app cache (using a rooted device).

The code here relies on Python 3.7
If running in Raspberry Pi, install Python 3.7 manually.
Also install additional libraries:
pip3.7 install dataclasses_json pycryptodome
"""

__author__ = 'droreiger@gmail.com (Dror Eiger)'

import argparse
import base64
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import enum
import hmac
from http.client import HTTPConnection
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
import logging.handlers
import math
import queue
import random
import socket
import string
import sys
import threading
import time
import typing
from urllib.parse import parse_qs, urlparse, ParseResult

from Crypto.Cipher import AES


@dataclass_json
@dataclass
class LanConfig:
  lanip_key: str
  lanip_key_id: int
  random_1: str
  time_1: int
  random_2: str
  time_2: int


@dataclass
class Encryption:
  sign_key: bytes
  crypto_key: bytes
  iv_seed: bytes
  cipher: AES
  
  def __init__(self, lanip_key: bytes, msg: bytes):
    self.sign_key = self._build_key(lanip_key, msg + b'0')
    self.crypto_key = self._build_key(lanip_key, msg + b'1')
    self.iv_seed = self._build_key(lanip_key, msg + b'2')[:AES.block_size]
    self.cipher = AES.new(self.crypto_key, AES.MODE_CBC, self.iv_seed)

  @classmethod
  def _build_key(cls, lanip_key: bytes, msg: bytes) -> bytes:
    return cls.hmac_digest(lanip_key, cls.hmac_digest(lanip_key, msg) + msg)
  
  @staticmethod
  def hmac_digest(key: bytes, msg: bytes) -> bytes:
    return hmac.digest(key, msg, 'sha256')


@dataclass
class Config:
  lan_config: LanConfig
  app: Encryption
  dev: Encryption
  
  def __init__(self):
    with open(_parsed_args.config, 'rb') as f:
      self.lan_config = LanConfig.from_json(f.read().decode('utf-8'))
    self._update_encryption()
    
  def update(self):
    """Updates the stored lan config, and encryption data."""
    with open(_parsed_args.config, 'wb') as f:
      f.write(self.lan_config.to_json().encode('utf-8'))
    self._update_encryption()

  def _update_encryption(self):
    lanip_key = self.lan_config.lanip_key.encode('utf-8')
    random_1 = self.lan_config.random_1.encode('utf-8')
    random_2 = self.lan_config.random_2.encode('utf-8')
    time_1 = str(self.lan_config.time_1).encode('utf-8')
    time_2 = str(self.lan_config.time_2).encode('utf-8')
    self.app = Encryption(lanip_key, random_1 + random_2 + time_1 + time_2)
    self.dev = Encryption(lanip_key, random_2 + random_1 + time_2 + time_1)

class Error(Exception):
  """Error class for AC handling."""
  pass

class AirFlow(enum.IntEnum):
  OFF = 0
  VERTICAL_ONLY = 1
  HORIZONTAL_ONLY = 2
  VERTICAL_AND_HORIZONTAL = 3

class FanSpeed(enum.IntEnum):
  AUTO = 0
  LOWER = 5
  LOW = 6
  MIDIUM = 7
  HIGH = 8
  HIGHER = 9

class SleepMode(enum.IntEnum):
  STOP = 0
  ONE = 1
  TWO = 2
  THREE = 3
  FOUR = 4

class StateMachine(enum.IntEnum):
  FANONLY = 0
  HEAT = 1
  COOL = 2
  DRY = 3
  AUTO = 4
  FAULTSHIELD = 5
  POWEROFF = 6
  OFFLINE = 7
  READONLYSHARED = 8

class WorkMode(enum.IntEnum):
  FAN = 0
  HEAT = 1
  COOL = 2
  DRY = 3
  AUTO = 4

class AirFlow(enum.Enum):
  OFF = 0
  ON = 1

class DeviceErrorStatus(enum.Enum):
  NORMALSTATE = 0
  FAULTSTATE = 1

class Dimmer(enum.Enum):
  ON = 0
  OFF = 1

class DoubleFrequency(enum.Enum):
  OFF = 0
  ON = 1

class Economy(enum.Enum):
  OFF = 0
  ON = 1

class EightHeat(enum.Enum):
  OFF = 0
  ON = 1

class FastColdHeat(enum.Enum):
  OFF = 0
  ON = 1

class Power(enum.Enum):
  OFF = 0
  ON = 1

class Quiet(enum.Enum):
  OFF = 0
  ON = 1

class TemperatureUnit(enum.Enum):
  CELSIUS = 0
  FAHRENHEIT = 1


@dataclass_json
@dataclass
class Properties:
  t_backlight: Dimmer = Dimmer.OFF  # DimmerStatus
  t_run_mode: DoubleFrequency = DoubleFrequency.OFF  # DoubleFrequency
  t_eco: Economy = Economy.OFF  # EconomyStatus
  t_temp_eight: EightHeat = EightHeat.OFF  # EightHeatStatus
  t_fan_leftright: AirFlow = AirFlow.OFF  # HorizontalAirFlow
  t_fan_speed: FanSpeed = FanSpeed.AUTO  # FanSpeed
  t_fan_power: AirFlow = AirFlow.OFF  # VerticalAirFlow
  t_temp_heatcold: FastColdHeat = FastColdHeat.OFF  # FastCoolHeatStatus
  f_humidity: int = 50  # Humidity
  f_temp_in: int = 81  # EnvironmentTemperature (Fahrenheit)
  t_power: Power = Power.ON  # PowerStatus
  t_fan_mute: Quiet = Quiet.OFF  # QuiteModeStatus
  t_sleep: SleepMode = SleepMode.STOP  # SleepMode
  t_temptype: TemperatureUnit = TemperatureUnit.FAHRENHEIT  # CurrentTemperatureUnit
  t_temp: int = 81  # CurrentTemperature
  t_work_mode: WorkMode = WorkMode.AUTO  # WorkModeStatus

@dataclass
class Data:
  """The current data store: commands, updates and properties."""
  commands_queue = queue.Queue()
  commands_seq_no = 0
  commands_seq_no_lock = threading.Lock()
  updates_seq_no = 0
  updates_seq_no_lock = threading.Lock()
  properties = Properties()
  properties_lock = threading.Lock()

def pad(data: bytes):
  """Zero padding for AES data encryption (non standard)."""
  new_size = math.ceil(len(data) / AES.block_size) * AES.block_size
  return data.ljust(new_size, bytes([0]))


def unpad(data: bytes):
  """Remove Zero padding for AES data encryption (non standard)."""
  return data.rstrip(bytes([0]))


class KeepAliveThread(threading.Thread):
  """Thread to preiodically generate keep-alive requests."""
  
  _INTERVAL = 10.0

  def __init__(self):
    self.run_lock = threading.Condition()
    sock = None
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      sock.connect(('10.255.255.255', 1))
      local_ip = sock.getsockname()[0]
    finally:
      if sock:
        sock.close()
    self._headers = {
      'Accept': 'application/json',
      'Connection': 'Keep-Alive',
      'Content-Type': 'application/json',
      'Host': _parsed_args.ip,
      'Accept-Encoding': 'gzip'
    }
    self._json = {
      'local_reg': {
        'ip': local_ip,
        'notify': 0,
        'port': _parsed_args.port,
        'uri': "/local_lan"
      }
    }
    super(KeepAliveThread, self).__init__(name='Keep Alive thread')

  def run(self) -> None:
    self.run_lock.acquire()
    conn = None
    while True:
      if not conn:
        conn = HTTPConnection(_parsed_args.ip)
        method = 'POST'
      else:
        method = 'PUT'
      logging.debug('%s /local_reg.json %s', method, json.dumps(self._json))
      conn.request(method, '/local_reg.json', json.dumps(self._json), self._headers)
      resp = conn.getresponse()
      if resp.status != 202:
        logging.error('Recieved invalid response for local_reg: %r', resp)
      self._json['local_reg']['notify'] = int(
          self.run_lock.wait(self._INTERVAL))


class HTTPRequestHandler(BaseHTTPRequestHandler):
  """Handler for AC related HTTP requests."""

  def do_HEAD(self, code: int = 200) -> None:
    """Return a JSON header."""
    self.send_response(code)
    if code == 200:
      self.send_header('Content-type', 'application/json')
      self.end_headers()

  def do_GET(self) -> None:
    """Accepts get requests."""
    logging.debug('GET Request,\nPath: %s\n', self.path)
    parsed_url = urlparse(self.path)
    query = parse_qs(parsed_url.query)
    handler = self._HANDLERS_MAP.get(parsed_url.path)
    if handler:
      handler(self, parsed_url.path, query, {})
      return
    self.do_HEAD(400)

  def do_POST(self):
    """Accepts post requests."""
    content_length = int(self.headers['Content-Length'])
    post_data = self.rfile.read(content_length)
    logging.debug('POST request,\nPath: %s\nHeaders:\n%s\n\nBody:\n%s\n',
                  str(self.path), str(self.headers), post_data.decode('utf-8'))
    parsed_url = urlparse(self.path)
    query = parse_qs(parsed_url.query)
    data = json.loads(post_data)
    handler = self._HANDLERS_MAP.get(parsed_url.path)
    if handler:
      handler(self, parsed_url.path, query, data)
      return
    self.do_HEAD(400)

  def key_exchange_handler(self, path: str, query: dict, data: dict) -> None:
    """Handles a key exchange.
    Accepts the AC's random and time and pass its own.
    Note that a key encryption component is the lanip_key, mapped to the
    lanip_key_id provided by the AC. This secret part is provided by HiSense
    server. Fortunately the lanip_key_id (and lanip_key) are static for a given
    AC.
    """
    try:
      key = data['key_exchange']
      if key['ver'] != 1 or key['proto'] != 1 or key.get('sec'):
        raise KeyError()
      _config.lan_config.random_1 = key['random_1']
      _config.lan_config.time_1 = key['time_1']
    except KeyError:
      logging.error('Invalid key exchange: %r', data)
      self.do_HEAD(400)
      return
    if key['key_id'] != _config.lan_config.lanip_key_id:
      logging.error('The key_id has been replaced!!\nOld ID was %d; new ID is %d.',
                    _config.lan_config.lanip_key_id, key['key_id'])
      self.do_HEAD(404)
      return
    _config.lan_config.random_2 = ''.join(
        random.choices(string.ascii_letters + string.digits, k=16))
    _config.lan_config.time_2 = time.monotonic_ns()
    _config.update()
    self.do_HEAD()
    self._write_json({"random_2": _config.lan_config.random_2,
                      "time_2": _config.lan_config.time_2})

  def command_handler(self, path: str, query: dict, data: dict) -> None:
    """Handles a command request.
    Request arrives from the AC. takes a command from the queue,
    builds the JSON, encrypts and signs it, and sends it to the AC.
    """
    command = {}
    with _data.commands_seq_no_lock:
      command['seq_no'] = _data.commands_seq_no
      _data.commands_seq_no += 1
    try:
      command['data'] = _data.commands_queue.get_nowait()
    except queue.Empty:
      command['data'] = {}
    self.do_HEAD()
    self._write_json(self._encrypt_and_sign(command))

  def property_update_handler(self, path: str, query: dict, data: dict) -> None:
    """Handles a property update request.
    Decrypts, validates, and pushes the value into the local properties store.
    """
    try:
      update = self._decrypt_and_validate(data)
    except Error as e:
      logging.error(str(e))
      self.do_HEAD(400)
      return
    self.do_HEAD()
    with _data.updates_seq_no_lock:
      if _data.updates_seq_no > update['seq_no']:
        return  # Old update
      _data.updates_seq_no = update['seq_no']
    try:
      name = update['data']['name']
      value = update['data']['value']
      with _data.properties_lock:
        data_type = type(getattr(_data.properties, name))
        setattr(_data.properties, name, data_type(value))
        logging.debug('Updated properties: %s' % _data.properties)
    except:
      logging.exception('Failed to handle %s', update)

  def _queue_command(self, name: str, value) -> None:
    with _data.properties_lock:
      curr_value = getattr(_data.properties, name)
    base_type = self._get_base_type(curr_value)
    data_type = type(curr_value)
    command = {
      'properties': [{
        'property': {
          'base_type': base_type,
          'name': name,
          'value': data_type(value)
        }
      }]
    }
    _data.commands_queue.put_nowait(command)

  @staticmethod
  def _get_base_type(value) -> str:
    if isinstance(data_type, int):
      return 'integer'
    if isinstance(data_type, float):
      return 'decimal'
    if isinstance(data_type, str):
      return 'string'
    return 'boolean'

  @staticmethod
  def _encrypt_and_sign(data: dict) -> dict:
    text = json.dumps(data).encode('utf-8')
    logging.debug('Encrypting: %s', text.decode('utf-8'))
    return {
      "enc": base64.b64encode(_config.app.cipher.encrypt(pad(text))).decode('utf-8'),
      "sign": base64.b64encode(Encryption.hmac_digest(_config.app.sign_key, text)).decode('utf-8')
    }

  @staticmethod
  def _decrypt_and_validate(data: dict) -> dict:
    text = unpad(_config.dev.cipher.decrypt(base64.b64decode(data['enc'])))
    sign = base64.b64encode(Encryption.hmac_digest(_config.dev.sign_key, text)).decode('utf-8')
    if sign != data['sign']:
      raise Error('Invalid signature for %s!' % text.decode('utf-8'))
    logging.debug('Decrypted: %s', text.decode('utf-8'))
    return json.loads(text.decode('utf-8'))

  def _write_json(self, data: dict) -> None:
    """Send out the provided data dict as JSON."""
    logging.debug('Response: %r', data)
    self.wfile.write(json.dumps(data).encode('utf-8'))

  _HANDLERS_MAP = {
    '/local_lan/key_exchange.json': key_exchange_handler,
    '/local_lan/commands.json': command_handler,
    '/local_lan/property/datapoint.json': property_update_handler,
    # TODO: Handle these if needed.
    # '/local_lan/property/datapoint/ack.json': property_update_handler,
    # '/local_lan/node/property/datapoint.json': property_update_handler,
    # '/local_lan/node/property/datapoint/ack.json': property_update_handler,
    # '/local_lan/node/conn_status.json': connection_status_handler,
    # '/local_lan/connect_status': module_request_handler,
    # '/local_lan/status.json': setup_device_details_handler,
    # '/local_lan/wifi_scan.json': module_request_handler,
    # '/local_lan/wifi_scan_results.json': module_request_handler,
    # '/local_lan/wifi_status.json': module_request_handler,
    # '/local_lan/regtoken.json': module_request_handler,
    # '/local_lan/wifi_stop_ap.json': module_request_handler
  }


def mqtt_on_connect(client: mqtt.Client, userdata, flags, rc):
  client.subscribe(_mqtt_topics['sub'])


def mqtt_on_message(client: mqtt.Client, userdata, message: mqtt.MQTTMessage):
  mqtt_publish_status(_data.commands_queue.put(json.loads(message.payload)))


def mqtt_publish_status(status: dict) -> None:
  if _mqtt_client:
    _mqtt_client.publish(_mqtt_topics['pub'],
                         payload=_data.properties.to_json().encode('utf-8'))


def ParseArguments() -> argparse.Namespace:
  """Parse command line arguments."""
  arg_parser = argparse.ArgumentParser(
      description='JSON server for HiSense air conditioners.',
      allow_abbrev=False)
  arg_parser.add_argument('-p', '--port', required=True, type=int,
                          help='Port for the server.')
  arg_parser.add_argument('--ip', required=True,
                          help='IP address for the AC.')
  arg_parser.add_argument('--config', required=True,
                          help='LAN Config file.')
  arg_parser.add_argument('--mqtt_host', default=None,
                          help='MQTT broker hostname or IP address.')
  arg_parser.add_argument('--mqtt_port', type=int, default=1883,
                          help='MQTT broker port.')
  arg_parser.add_argument('--mqtt_client_id', default=None,
                          help='MQTT client ID.')
  arg_parser.add_argument('--mqtt_user', default=None,
                          help='<user:password> for the MQTT channel.')
  arg_parser.add_argument('--mqtt_topic', default='hisense_ac',
                          help='MQTT topic.')
  arg_parser.add_argument('--log_level', default='WARNING',
                          choices={'CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'},
                          help='Minimal log level.')
  return arg_parser.parse_args()


if __name__ == '__main__':
  _parsed_args = ParseArguments()  # type: argparse.Namespace

  log_socket = '/var/run/syslog' if sys.platform == 'darwin' else '/dev/log'
  logging_handler = logging.handlers.SysLogHandler(address=log_socket)
  logging_handler.setFormatter(
      logging.Formatter(fmt='{levelname[0]}{asctime}.{msecs:03.0f}  '
                        '{filename}:{lineno}] {message}',
                         datefmt='%m%d %H:%M:%S', style='{'))
  logger = logging.getLogger()
  logger.setLevel(_parsed_args.log_level)
  logger.addHandler(logging_handler)

  _config = Config()
  _data = Data()

  _keep_alive = KeepAliveThread()
  _keep_alive.start()

  _mqtt_client = None  # type: typing.Optional[mqtt.Client]
  _mqtt_topics = {}  # type: typing.Dict[str, str]
  if _parsed_args.mqtt_host:
    _mqtt_topics['pub'] = os.path.join(_parsed_args.mqtt_topic, 'status')
    _mqtt_topics['sub'] = os.path.join(_parsed_args.mqtt_topic, 'command')
    _mqtt_client = mqtt.Client(client_id=_parsed_args.mqtt_client_id,
                               clean_session=True)
    _mqtt_client.on_connect = mqtt_on_connect
    _mqtt_client.on_message = mqtt_on_message
    if _parsed_args.mqtt_user:
      _mqtt_client.username_pw_set(*_parsed_args.mqtt_user.split(':',1))
    _mqtt_client.connect(_parsed_args.mqtt_host, _parsed_args.mqtt_port)
    _mqtt_client.loop_start()

  httpd = HTTPServer(('', _parsed_args.port), HTTPRequestHandler)
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  httpd.server_close()