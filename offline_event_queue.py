"""
A queue of events that happened offline and that should be replayed once back online.
"""

import json
import os
import traceback

from external_rtc import ExternalRTC

# noinspection PyBroadException
try:
	from typing import List, Callable, Optional
except:
	pass

from api import APIRequest, GetAllTimersAPIRequest
from sdcard import SDCard

class OfflineEventQueue:
	"""
	A queue of events that were generated while the BabyPod was offline and will be replayed once back online.

	The queue is stored as individual JSON files for each event named after the date/time they occurred. As the queue
	replays, each successful replay of an event deletes that event from the queue.
	"""

	@staticmethod
	def from_sdcard(sdcard: SDCard, rtc: ExternalRTC):
		"""
		Gets an instance of the event queue given an SD card that stores it and an RTC used for timing data.

		:param sdcard: SD card for storing this queue
		:param rtc: RTC for timing data
		:return: OfflineEventQueue instance
		"""

		return OfflineEventQueue(sdcard.get_absolute_path("queue"), rtc)

	def __init__(self, json_path: str, rtc: ExternalRTC):
		"""
		Starts a new queue or resumes an existing one at the given directory containing JSON files.

		Use get_instance() to respect the use of the SD card vs. guessing at paths.

		:param json_path: Directory that contains, or will contain, the JSON queue
		:param rtc: RTC for timing data
		"""
		self.json_path = json_path
		self.rtc = rtc

		try:
			os.stat(self.json_path)
		except OSError:
			print(f"Creating new offline event queue at {self.json_path}")
			os.mkdir(self.json_path)

	def get_json_files(self) -> List[str]:
		"""
		Gets a list of all JSON files in this queue sorted by their origination date in ascending order. Use this along
		with replay() on each file returned to replay the queue in order.

		:return: All JSON filenames in the queue (which could be an empty list)
		"""

		files = os.listdir(self.json_path)
		files.sort()
		return list(map(lambda filename: f"{self.json_path}/{filename}", files))

	def build_json_filename(self) -> str:
		"""
		Creates a filename for storing a JSON file based on the current date/time. In the unlikely event of a conflict,
		then an increasing number is added to the end of the file. In the ridiculously unlikely event of a conflict
		after many attempts to avoid one, raises a ValueError. Something would have gone horribly wrong for that to
		happen.

		:return: JSON filename for an event, like /sd/queue/20241015224103-0001.json
		"""

		now = self.rtc.now()
		formatted_now = f"{now.year:04}{now.month:02}{now.day:02}{now.hour:02}{now.minute:02}{now.second:02}"

		i = 0
		while i < 1000:
			filename = self.json_path + f"/{formatted_now}-{i:04}.json"
			try:
				os.stat(filename)
				# if stat() passes, then the file already exists; try again with next index
				i += 1
			except OSError: # stat() failed, which means file doesn't exist (hopefully) and is a good candidate
				return filename

		raise ValueError("No candidate files available, somehow")

	def add(self, request: APIRequest) -> None:
		"""
		Adds an event to the queue.

		:param request: Request to serialize to replay later
		"""

		payload = {
			"type": type(request).__name__,
			"payload": request.serialize_to_json()
		}

		filename = self.build_json_filename()

		with open(filename, "w") as file:
			# noinspection PyTypeChecker
			json.dump(payload, file)
			file.flush()

	# TODO making this dynamic with reflection would be nice but I don't think CircuitPython can
	@staticmethod
	def init_api_request(class_name: str, payload) -> APIRequest:
		"""
		Creates a concrete APIRequest instance given the JSON payload of an event in the queue
		:param class_name: Class name of the APIRequest concrete type, like "PostFeedingAPIRequest"
		:param payload: JSON payload of an event in the queue
		:return: Concrete APIRequest instance that can be invoke()d
		"""

		if class_name == "PostFeedingAPIRequest":
			from api import PostFeedingAPIRequest
			return PostFeedingAPIRequest.deserialize_from_json(payload)
		elif class_name == "PostChangeAPIRequest":
			from api import PostChangeAPIRequest
			return PostChangeAPIRequest.deserialize_from_json(payload)
		elif class_name == "PostPumpingAPIRequest":
			from api import PostPumpingAPIRequest
			return PostPumpingAPIRequest.deserialize_from_json(payload)
		elif class_name == "PostTummyTimeAPIRequest":
			from api import PostTummyTimeAPIRequest
			return PostTummyTimeAPIRequest.deserialize_from_json(payload)
		elif class_name == "PostSleepAPIRequest":
			from api import PostSleepAPIRequest
			return PostSleepAPIRequest.deserialize_from_json(payload)
		else:
			raise ValueError(f"Don't know how to deserialize a {class_name}")

	def replay_all(self,
		on_replay: Callable[[int, int], None] = None,
		on_failed_event: Callable[[Optional[APIRequest]], bool] = None,
		delete_on_success: bool = True
	) -> None:
		"""
		Replay all events that are stored in the queue.

		:param on_replay: Do this just before replaying an event, like updating a progress bar; callback is given the
		index of the event being replayed and the total number of events being replayed
		:param on_failed_event: Do this when an event fails; callback is given the event that failed to replay and must
		return True to delete the failed event and continue or False to keep the event in the queue and keep going. If
		the callback is None, then failed events raise exceptions.
		:param delete_on_success: Delete events that successfully replay
		"""

		index = 0
		files = self.get_json_files()
		if not files:
			return # nothing to do

		# check for existing timers in case an API payload refers to an ID that doesn't exist
		existing_timer_ids = [int(timer.timer_id) for timer in GetAllTimersAPIRequest().get_active_timers()]

		for full_json_path in files:
			if on_replay is not None:
				on_replay(index, len(files))
			with open(full_json_path, "r") as file:
				item = json.load(file)

			delete = delete_on_success
			request = None
			try:
				with open(full_json_path, "r") as file:
					print(f"Replaying {full_json_path}: {file.read()}")
				request = self.init_api_request(item["type"], item["payload"])

				if request.payload is not None and "timer" in request.payload:
					timer_id = request.payload["timer"]
					if timer_id not in existing_timer_ids:
						print(f"Removing obsolete reference to timer ID {timer_id}")
						del request.payload["timer"]
					elif "start" in request.payload and "end" in request.payload:
						print(f"Timer ID {timer_id} is still active, removing references to start/end")
						del request.payload["start"]
						del request.payload["end"]

				request.invoke()
			except Exception as e:
				traceback.print_exception(e)
				if on_failed_event is not None:
					delete = on_failed_event(request)
				else:
					raise e
			if delete:
				os.unlink(full_json_path)