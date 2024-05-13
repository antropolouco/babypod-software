import board
import busio
from adafruit_character_lcd.character_lcd_i2c import Character_LCD_I2C

i2c = busio.I2C(scl = board.SCL, sda = board.SDA, frequency = 400000)
lcd = Character_LCD_I2C(i2c, 20, 4)

lcd.message = "Starting up..."

from nvram import NVRAMValues

from backlight import Backlight
backlight = Backlight(NVRAMValues.OPTION_BACKLIGHT.get())

from piezo import Piezo
piezo = Piezo(NVRAMValues.OPTION_PIEZO.get())
piezo.tone("startup")

from digitalio import DigitalInOut, Direction
from battery_monitor import BatteryMonitor
from rotary_encoder import RotaryEncoder
from flow import Flow
from lcd_special_chars_module import LCDSpecialChars
import supervisor

supervisor.runtime.autoreload = False

# turn off Neopixel
neopixel = DigitalInOut(board.NEOPIXEL) #board.LDO2)
neopixel.direction = Direction.OUTPUT
neopixel.value = False

battery_monitor = BatteryMonitor.get_instance(i2c)
rotary_encoder = RotaryEncoder(i2c)
lcd_special_chars = LCDSpecialChars(lcd)

Flow(
	lcd_dimensions = (20, 4),
	lcd = lcd,
	child_id = NVRAMValues.CHILD_ID.get(),
	rotary_encoder = rotary_encoder,
	battery_monitor = battery_monitor,
	backlight = backlight,
	piezo = piezo,
	lcd_special_chars = lcd_special_chars
).start()
