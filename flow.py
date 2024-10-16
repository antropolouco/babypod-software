import os
import traceback

import microcontroller

from api import GetFirstChildIDAPIRequest, GetLastFeedingAPIRequest, PostChangeAPIRequest, Timer, \
	PostFeedingAPIRequest, PostPumpingAPIRequest, PostTummyTimeAPIRequest, PostSleepAPIRequest, \
	APIRequestFailedException, GetAPIRequest, PostAPIRequest, DeleteAPIRequest, GetAllTimersAPIRequest, TimerAPIRequest, \
	ConnectionManager, ConsumeMOTDAPIRequest, FeedingAPIRequest
from devices import Devices
from lcd import LCD, BacklightColors
from nvram import NVRAMValues
from offline_event_queue import OfflineEventQueue
from offline_state import OfflineState
from periodic_chime import EscalatingIntervalPeriodicChime, ConsistentIntervalPeriodicChime, PeriodicChime
from setting import Setting
from ui_components import NumericSelector, VerticalMenu, VerticalCheckboxes, ActiveTimer, ProgressBar, Modal, \
	StatusMessage, NoisyBrightModal, SuccessModal, ErrorModal, UIComponent
from user_input import ActivityListener, WaitTickListener, ShutdownRequestListener, ResetRequestListener
from util import Util

# noinspection PyBroadException
try:
	from typing import Optional, cast
except:
	pass
	# ignore, just for IDE's sake, not supported on board

class Flow:
	def __init__(self, devices: Devices):
		self.requests = None
		self.child_id = None
		self.devices = devices

		self.suppress_idle_warning = False

		self.devices.rotary_encoder.on_activity_listeners.append(ActivityListener(
			on_activity = self.on_user_input
		))

		if self.devices.power_control is not None:
			self.devices.rotary_encoder.on_shutdown_requested_listeners.append(ShutdownRequestListener(
				on_shutdown_requested = self.on_shutdown_requested
			))

		self.devices.rotary_encoder.on_reset_requested_listeners.append(ResetRequestListener(
			on_reset_requested = self.on_reset_requested
		))

		self.devices.rotary_encoder.on_wait_tick_listeners.extend([
			WaitTickListener(
				on_tick = self.on_backlight_dim_idle,
				seconds = NVRAMValues.BACKLIGHT_DIM_TIMEOUT.get(),
				name = "Backlight dim idle"
			),
			WaitTickListener(
				on_tick = self.idle_warning,
				seconds = NVRAMValues.IDLE_WARNING.get(),
				recurring = True,
				name = "Idle warning"
			),
			WaitTickListener(
				on_tick = lambda _: UIComponent.refresh_battery_percent(devices = self.devices, only_if_changed = True),
				seconds = 30,
				recurring = True,
				name = "On idle tick"
			)
		])

		idle_shutdown = NVRAMValues.IDLE_SHUTDOWN.get()
		if self.devices.power_control and idle_shutdown:
			listener = WaitTickListener(
				on_tick = self.idle_shutdown,
				seconds = idle_shutdown,
				name = "Idle shutdown"
			)
			self.devices.rotary_encoder.on_wait_tick_listeners.append(listener)

		if self.devices.rtc is None or self.devices.sdcard is None:
			self.offline_state = None
			self.offline_queue = None
		else:
			self.offline_state = OfflineState.from_sdcard(self.devices.sdcard)
			self.devices.rtc.offline_state = self.offline_state

			self.offline_queue = OfflineEventQueue.from_sdcard(self.devices.sdcard, self.devices.rtc)

		self.use_offline_feeding_stats = bool(NVRAMValues.OFFLINE)
		self.device_name = os.getenv("DEVICE_NAME") or "BabyPod"

		self.is_shutting_down = False

	def on_shutdown_requested(self) -> None:
		self.is_shutting_down = True
		self.devices.power_control.shutdown()

	def on_reset_requested(self) -> None:
		ErrorModal(
			devices = self.devices,
			message = "Resetting"
		).render() # but not wait
		microcontroller.reset()

	def on_backlight_dim_idle(self, _: float) -> None:
		if self.devices.lcd.backlight.color == BacklightColors.DEFAULT:
			self.devices.lcd.backlight.set_color(BacklightColors.DIM)

	def idle_warning(self, _: float) -> None:
		if not self.suppress_idle_warning:
			self.devices.piezo.tone("idle_warning")

	def idle_shutdown(self, _: float) -> None:
		if not self.suppress_idle_warning:
			print("Idle; soft shutdown")
			self.devices.power_control.shutdown(silent = True)

	def on_user_input(self) -> None:
		if self.devices.lcd.backlight.color == BacklightColors.DIM:
			self.devices.lcd.backlight.set_color(BacklightColors.DEFAULT)

	def refresh_rtc(self) -> None:
		if NVRAMValues.OFFLINE:
			print("Going online for next reboot")
			NVRAMValues.OFFLINE.write(False)
			raise ValueError("RTC must be set before going offline")

		StatusMessage(devices = self.devices, message = "Setting clock...").render()

		try:
			old_now = self.devices.rtc.now()
			self.devices.rtc.sync(self.requests)
			if old_now is not None:
				print(f"RTC drift since last sync: {old_now - self.devices.rtc.now()}")
		except Exception as e:
			print(f"{e} when syncing RTC; forcing sync on next online boot")
			NVRAMValues.FORCE_RTC_UPDATE.write(True)
			raise e

	def auto_connect(self) -> None:
		if not NVRAMValues.OFFLINE:
			StatusMessage(devices = self.devices, message = "Connecting...").render()
			# noinspection PyBroadException
			try:
				self.requests = ConnectionManager.connect()
			except Exception as e:
				import traceback
				traceback.print_exception(e)
				if self.devices.rtc and self.devices.sdcard:
					self.offline()
				else:
					raise e # can't go offline automatically because there's no hardware support
		elif not self.devices.rtc:
			raise ValueError("External RTC is required for offline support")
		else:
			print("Working offline")

	def init_rtc(self) -> None:
		if self.devices.rtc:
			if NVRAMValues.FORCE_RTC_UPDATE:
				print("RTC update forced")
				self.refresh_rtc()
			elif not self.devices.rtc.now():
				print("RTC not set or is implausible")
				self.refresh_rtc()
			elif self.offline_state.last_rtc_set is None:
				print("Last RTC set date/time unknown; assuming now")
			else:
				now = self.devices.rtc.now()
				last_rtc_set_delta = now - self.offline_state.last_rtc_set
				if last_rtc_set_delta.seconds >= 60 * 60 * 24 or last_rtc_set_delta.days >= 1:
					print("RTC last set more than a day ago")

					if NVRAMValues.OFFLINE:
						print("RTC will be updated next time device is online")
					else:
						print("RTC refresh interval expired")
						self.refresh_rtc()

	def init_battery(self) -> None:
		if self.devices.battery_monitor:
			battery_percent = self.devices.battery_monitor.get_percent()
			if battery_percent is not None and battery_percent <= 15:
				NoisyBrightModal(
					devices = self.devices,
					piezo_tone = "low_battery",
					message = "Low battery!",
					color = BacklightColors.ERROR,
					auto_dismiss_after_seconds = 2).render().wait()

	def start(self) -> None:
		self.device_startup()

		self.init_child_id()
		self.jump_to_running_timer()
		self.check_motd()
		self.loop()

	def check_motd(self) -> None:
		if self.devices.rtc and not NVRAMValues.OFFLINE:
			now = self.devices.rtc.now()
			last_checked = self.offline_state.last_motd_check

			if last_checked is not None:
				delta = now - last_checked
				# noinspection PyUnresolvedReferences
				delta_seconds = delta.seconds + (delta.days * 60 * 60 * 24)
				motd_check_required = delta_seconds >= int(NVRAMValues.MOTD_CHECK_INTERVAL)
			else:
				motd_check_required = True

			if motd_check_required:
				try:
					StatusMessage(devices = self.devices, message = "Checking messages...").render()
					motd = ConsumeMOTDAPIRequest().get_motd()

					if motd is not None:
						NoisyBrightModal(
							devices = self.devices,
							message = motd,
							piezo_tone = "motd"
						).render().wait()

					self.offline_state.last_motd_check = now
					self.offline_state.to_sdcard()
				except Exception as e:
					import traceback
					traceback.print_exception(e)
					print(f"Getting MOTD failed: {e}")

	def device_startup(self) -> None:
		self.auto_connect()
		self.init_rtc()
		self.init_battery()

	def init_child_id(self) -> None:
		child_id = NVRAMValues.CHILD_ID.get()
		if not child_id:
			StatusMessage(devices = self.devices, message = "Getting children...").render()
			try:
				child_id = GetFirstChildIDAPIRequest().get_first_child_id()
			except Exception as e:
				self.on_error(e)
				print("Child discovery failed so just guessing ID 1")
				child_id = 1
			NVRAMValues.CHILD_ID.write(child_id)
		self.child_id = child_id

	def loop(self) -> None:
		while True:
			try:
				self.main_menu()
			except Exception as e:
				self.on_error(e)

	def jump_to_running_timer(self) -> None:
		timer = None

		if not NVRAMValues.OFFLINE:
			timer = self.check_for_running_timer()

		if timer is not None:
			timer_map = {
				"feeding": self.feeding,
				"sleep": self.sleep,
				"tummy_time": self.tummy_time,
				"pumping": self.pumping
			}

			for name, _ in timer_map.items():
				if timer.name == TimerAPIRequest.get_timer_name(name):
					try:
						timer_map[name](timer)
					except Exception as e:
						self.on_error(e)

					break

	def check_for_running_timer(self) -> Optional[Timer]:
		timer = None
		try:
			StatusMessage(devices = self.devices, message = "Checking timers...").render()
			timers = list(GetAllTimersAPIRequest(limit = 1).get_active_timers())
			if timers:
				timer = timers[0]
		except Exception as e:
			print(f"Failed getting active timers; continuing to main menu: {e}")
		return timer

	def on_error(self, e: Exception) -> None:
		traceback.print_exception(e)
		message = f"Got {type(e).__name__}!"
		if isinstance(e, APIRequestFailedException):
			request = e.request

			if isinstance(request, GetAPIRequest):
				message = "GET"
			elif isinstance(request, PostAPIRequest):
				message = "POST"
			elif isinstance(request, DeleteAPIRequest):
				message = "DELETE"
			else:
				message = "Request"

			message += " failed"
			if e.http_status_code != 0:
				message += f" ({e.http_status_code})"
		elif "ETIMEDOUT" in str(e):
				message = "Request timeout!"

		ErrorModal(devices = self.devices, message = message).render().wait()

	def render_success_splash(self, message: str = "Saved!", is_stopped_timer: bool = False) -> None:
		SuccessModal(devices = self.devices, message = message).render().wait()

		if is_stopped_timer and NVRAMValues.AUTO_OFF_AFTER_TIMER_SAVED:
			response = Modal(
				devices = self.devices,
				message = "Auto shutdown in 10 seconds...",
				save_text = "Keep on",
				auto_dismiss_after_seconds = 10
			).render().wait()

			if not response:
				self.devices.power_control.shutdown(silent = True)

	def main_menu(self) -> None:
		if self.use_offline_feeding_stats or NVRAMValues.OFFLINE:
			last_feeding = self.offline_state.last_feeding
			method = self.offline_state.last_feeding_method

			# reapply the value which could have been changed by feeding saved just now
			self.use_offline_feeding_stats = bool(NVRAMValues.OFFLINE)
		else:
			StatusMessage(devices = self.devices, message = "Getting feeding...").render()
			try:
				last_feeding, method = GetLastFeedingAPIRequest(self.child_id).get_last_feeding()
			except Exception as e:
				print(f"Failed getting last feeding: {e}")
				traceback.print_exception(e)
				last_feeding = None
				method = None

			if self.offline_state is not None and \
					(self.offline_state.last_feeding != last_feeding or
					self.offline_state.last_feeding_method != method):
				self.offline_state.last_feeding = last_feeding
				self.offline_state.last_feeding_method = method
				self.offline_state.to_sdcard()

		if last_feeding is not None:
			last_feeding_str = "Feed " + Util.datetime_to_time_str(last_feeding)

			if method == "right breast":
				last_feeding_str += " R"
			elif method == "left breast":
				last_feeding_str += " L"
			elif method == "both breasts":
				last_feeding_str += " RL"
			elif method == "bottle":
				last_feeding_str += " B"
		else:
			last_feeding_str = "Feeding"

		menu_items = [
			(last_feeding_str, self.feeding),
			("Diaper change", self.diaper),
			("Sleep", self.sleep),
			("Pumping", self.pumping)
		]

		selected_index = VerticalMenu(
			header = "Main menu",
			options = [item[0] for item in menu_items],
			devices = self.devices,
			cancel_align = UIComponent.RIGHT,
			cancel_text = self.devices.lcd[LCD.UNCHECKED if NVRAMValues.OFFLINE else LCD.CHECKED],
			save_text = None
		).render().wait()

		if selected_index is None:
			self.settings()
		else:
			_, method = menu_items[selected_index]
			method()

	def settings(self) -> None:
		all_settings = [
			Setting(
				name = "Off after timers",
				backing_nvram_value = NVRAMValues.AUTO_OFF_AFTER_TIMER_SAVED,
				is_available = lambda: self.devices.power_control is not None
			),
			Setting(
				name = "Play sounds",
				backing_nvram_value = NVRAMValues.PIEZO
			),
			Setting(
				name = "Offline",
				backing_nvram_value = NVRAMValues.OFFLINE,
				is_available = lambda: self.devices.rtc is not None and self.devices.sdcard is not None,
				on_save = lambda going_offline: self.offline() if going_offline else self.back_online()
			)
		]

		settings = [setting for setting in all_settings if setting.is_available()]

		if len(settings) == 0:
			print("Warning: no settings available!")
			return

		responses = VerticalCheckboxes(
			header = "Settings",
			options = [setting.name for setting in settings],
			initial_states = [setting.get() for setting in settings],
			devices = self.devices,
			cancel_align = UIComponent.RIGHT
		).render().wait()

		if responses is not None:
			assert(len(responses) == len(settings))
			for i in range(0, len(responses)):
				setting = settings[i]
				value = responses[i]
				setting.save(value)

	def offline(self):
		NoisyBrightModal(
			devices = self.devices,
			message = "Going offline",
			piezo_tone = "info", auto_dismiss_after_seconds = 1
		).render().wait()
		ConnectionManager.disconnect()

	def back_online(self) -> None:
		NVRAMValues.OFFLINE.write(False)
		self.auto_connect()
		files = self.offline_queue.get_json_files()
		if len(files) > 0:
			print(f"Replaying offline-serialized {len(files)} requests")

			self.devices.lcd.clear()

			progress_bar = ProgressBar(devices = self.devices, count = len(files), message = "Syncing changes...")
			progress_bar.render()

			index = 0
			for filename in files:
				progress_bar.set_index(index)
				try:
					self.offline_queue.replay(filename)
				except Exception as e:
					NVRAMValues.OFFLINE.write(True)
					raise e
				index += 1

			self.render_success_splash("Change synced!" if len(files) == 1 else f"{len(files)} changes synced!")

	def diaper(self) -> None:
		selected_index = VerticalMenu(
			header = "How was diaper?",
			options = [
				"Wet",
				"Solid",
				"Both"
			],
			devices = self.devices
		).render().wait()

		if selected_index is not None:
			is_wet = selected_index == 0 or selected_index == 2
			is_solid = selected_index == 1 or selected_index == 2

			request = PostChangeAPIRequest(
				child_id = self.child_id,
				is_wet = is_wet,
				is_solid = is_solid
			)
			if NVRAMValues.OFFLINE:
				self.offline_queue.add(request)
			else:
				StatusMessage(devices = self.devices, message = "Saving...").render()
				request.invoke()
			self.render_success_splash()

	def pumping(self, existing_timer: Optional[Timer] = None) -> None:
		saved = False
		while not saved:
			timer = self.start_or_resume_timer(
				existing_timer = existing_timer,
				header_text = "Pumping",
				timer_name = "pumping",
				periodic_chime = ConsistentIntervalPeriodicChime(
					devices = self.devices,
					chime_at_seconds = 5 * 60
				)
			)

			if timer is not None:
				amount = NumericSelector(
					header = "How much?",
					devices = self.devices,
					minimum = 0,
					step = 0.5,
					format_str = "%.1f fl oz"
				).render().wait()

				if amount is not None:
					request = PostPumpingAPIRequest(
						child_id = self.child_id,
						timer = timer,
						amount = amount
					)
					if NVRAMValues.OFFLINE:
						self.offline_queue.add(request)
					else:
						StatusMessage(devices = self.devices, message = "Saving...").render()
						request.invoke()
					self.render_success_splash(is_stopped_timer = True)
					saved = True
			else:
				return

	def start_or_resume_timer(self,
		header_text: str,
		timer_name: str,
		periodic_chime: PeriodicChime = None,
		subtext: str = None,
		existing_timer: Optional[Timer] = None,
	) -> Optional[Timer]:
		if existing_timer is not None:
			timer = existing_timer
		elif NVRAMValues.OFFLINE:
			timer = Timer(
				name = timer_name,
				offline = True,
				rtc = self.devices.rtc,
				battery = self.devices.battery_monitor
			)
			timer.started_at = self.devices.rtc.now()
			timer.start_or_resume()
		else:
			StatusMessage(devices = self.devices, message = "Checking timers...").render()
			timer = Timer(
				name = timer_name,
				offline = False,
				battery = self.devices.battery_monitor
			)
			timer.start_or_resume()

		if subtext is not None:
			self.devices.lcd.write(message = subtext, coords = (0, 2))

		self.suppress_idle_warning = True
		response = ActiveTimer(
			header = header_text,
			devices = self.devices,
			periodic_chime = periodic_chime,
			start_at = timer.resume_from_duration
		).render().wait()
		self.suppress_idle_warning = False

		if response is None:
			if not NVRAMValues.OFFLINE:
				StatusMessage(devices = self.devices, message = "Stopping timer...").render()
			timer.cancel()
			return None # canceled

		return timer

	def feeding(self, existing_timer: Optional[Timer] = None) -> None:
		saved = False
		while not saved:
			timer = self.start_or_resume_timer(
				existing_timer = existing_timer,
				header_text = "Feeding",
				timer_name = "feeding",
				periodic_chime = EscalatingIntervalPeriodicChime(
					devices = self.devices,
					chime_at_seconds = 60 * 15,
					escalating_chime_at_seconds = 60 * 30,
					interval_once_escalated_seconds = 60
				)
			)

			if timer is not None:
				saved = self.save_feeding(timer)
			else:
				return # canceled the timer

	def save_feeding(self, timer: Timer) -> bool:
		enabled_food_types = NVRAMValues.ENABLED_FOOD_TYPES_MASK.get()

		options = []
		for food_type in FeedingAPIRequest.FOOD_TYPES:
			if food_type["mask"] & enabled_food_types:
				options.append(food_type)

		if not options:
			raise ValueError(f"All food types excluded by ENABLED_FOOD_TYPES_MASK bitmask {enabled_food_types}")

		if len(options) == 1:
			food_type_metadata = options[0]
		else:
			selected_index: Optional[int] = VerticalMenu(
				header = "What was fed?",
				devices = self.devices,
				options = list(map(lambda item: item["name"], options))
			).render().wait()

			if selected_index is None:
				return False

			food_type_metadata = options[selected_index]

		food_type = food_type_metadata["type"]

		method = None
		if len(food_type_metadata["methods"]) == 1:
			method = food_type_metadata["methods"][0]
		else:
			method_names = []
			for available_method in FeedingAPIRequest.FEEDING_METHODS:
				for allowed_method in food_type_metadata["methods"]:
					if available_method["method"] == allowed_method:
						method_names.append(available_method["name"])

			selected_index = VerticalMenu(
				header = "How was this fed?",
				devices = self.devices,
				options = method_names
			).render().wait()

			if selected_index is None:
				return False

			selected_method_name = method_names[selected_index]
			for available_method in FeedingAPIRequest.FEEDING_METHODS:
				if available_method["name"] == selected_method_name:
					method = available_method["method"]
					break

		request = PostFeedingAPIRequest(
			child_id = self.child_id,
			timer = timer,
			food_type = food_type,
			method = method
		)
		if NVRAMValues.OFFLINE:
			self.offline_queue.add(request)
		else:
			StatusMessage(devices = self.devices, message = "Saving...").render()
			request.invoke()

		if self.offline_state is not None:
			self.offline_state.last_feeding = timer.started_at
			self.offline_state.last_feeding_method = method
			self.offline_state.to_sdcard()
			self.use_offline_feeding_stats = True

		self.render_success_splash(is_stopped_timer = True)

		return True

	def sleep(self, existing_timer: Optional[Timer] = None) -> None:
		timer = self.start_or_resume_timer(
			existing_timer = existing_timer,
			header_text = "Sleep",
			timer_name = "sleep"
		)

		if timer is not None:
			request = PostSleepAPIRequest(child_id = self.child_id, timer = timer)
			if NVRAMValues.OFFLINE:
				self.offline_queue.add(request)
			else:
				StatusMessage(devices = self.devices, message = "Saving...").render()
				request.invoke()

			self.render_success_splash(is_stopped_timer = True)

	def tummy_time(self, existing_timer: Optional[Timer] = None) -> None:
		timer = self.start_or_resume_timer(
			existing_timer = existing_timer,
			header_text = "Tummy time",
			timer_name = "tummy_time",
			periodic_chime = ConsistentIntervalPeriodicChime(
				devices = self.devices,
				chime_at_seconds = 60
			)
		)

		if timer is not None:
			request = PostTummyTimeAPIRequest(child_id = self.child_id, timer = timer)
			if NVRAMValues.OFFLINE:
				self.offline_queue.add(request)
			else:
				StatusMessage(devices = self.devices, message = "Saving...").render()
				request.invoke()
			self.render_success_splash(is_stopped_timer = True)