import supervisor
supervisor.runtime.autoreload = False

from busio import I2C
import board
# noinspection PyUnresolvedReferences
i2c = I2C(sda = board.SDA, scl = board.SCL, frequency = 400000)

from lcd import LCD
lcd = LCD.get_instance(i2c)
lcd.write("Starting up...", (0, 0))

from piezo import Piezo
piezo = Piezo()
piezo.tone("startup")

from backlight import Backlight
backlight = Backlight.get_instance()

from digitalio import DigitalInOut, Direction

# turn off Neopixel
# noinspection PyUnresolvedReferences
neopixel = DigitalInOut(board.NEOPIXEL)
neopixel.direction = Direction.OUTPUT
neopixel.value = False

from battery_monitor import BatteryMonitor
battery_monitor = BatteryMonitor.get_instance(i2c)

from user_input import UserInput
user_input = UserInput.get_instance(i2c)

from external_rtc import ExternalRTC
rtc = ExternalRTC(i2c) if ExternalRTC.exists(i2c) else None

if rtc is not None:
	from sdcard import SDCard
	try:
		sdcard = SDCard()
	except Exception as e:
		print(f"Error while trying to mount SD card, assuming hardware is missing: {e}")
		sdcard = None
		from nvram import NVRAMValues
		NVRAMValues.OFFLINE.write(False)

from devices import Devices
devices = Devices(
	user_input = user_input,
	piezo = piezo,
	lcd = lcd,
	backlight = backlight,
	battery_monitor = battery_monitor,
	sdcard = sdcard,
	rtc = rtc
)

from flow import Flow
Flow(devices = devices).start()
