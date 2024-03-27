import re
import typing

import dockerfile
import frappe
from frappe.utils import now_datetime

# Reference:
# https://stackoverflow.com/questions/14693701/how-can-i-remove-the-ansi-escape-sequences-from-a-string-in-python
ansi_escape_rx = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

if typing.TYPE_CHECKING:
	from re import Match
	from typing import Any, Generator, TypedDict

	from press.press.doctype.deploy_candidate.deploy_candidate import DeployCandidate
	from press.press.doctype.deploy_candidate_build_step.deploy_candidate_build_step import (
		DeployCandidateBuildStep,
	)

	BuildOutput = list[str] | Generator[str, Any, None]
	IndexSplit = TypedDict(
		"IndexSplit",
		{
			"index": int,
			"line": str,
			"is_unusual": bool,
		},
	)


class DockerBuildOutputParser:
	"""
	Parses `docker build` raw output and updates Deploy Candidate.

	Output can be generated from a remote builder (agent) or from a build running
	on press itself.

	In case of a remote build, due to the way agent updates are propagated, all
	lines are updated when agent is polled, and so output is looped N! times.
	"""

	def __init__(self, dc: "DeployCandidate") -> None:
		self.dc = dc
		self.is_remote = dc.is_docker_remote_builder_used
		self.last_updated = now_datetime()

		# Used to generate output and track parser state
		self.lines: list[str] = []
		self.steps: dict[int, "DeployCandidateBuildStep"] = frappe._dict()

		# Convenience maps used to update build steps
		self.dc_steps_by_index = {bs.step_index: bs for bs in dc.build_steps}
		self.dc_steps_by_step_slug = {bs.step_slug: bs for bs in dc.build_steps}

	# `output` can be from local or remote build
	def parse(self, output: "BuildOutput"):
		for raw_line in output:
			self._parse_line_handle_exc(raw_line)
		self._end_parsing()

	def _parse_line_handle_exc(self, raw_line: str):
		try:
			self._parse_line(raw_line)
			self._update_dc_build_output()
		except Exception:
			import traceback

			print("Error in parsing line:", raw_line)
			traceback.print_exc()

	def _update_dc_build_output(self):
		if self.is_remote:
			return

		sec_since_last_update = (now_datetime() - self.last_updated).total_seconds()
		if sec_since_last_update <= 1:
			return

		self.build_output = "".join(self.lines)
		self.save(ignore_version=True)
		frappe.db.commit()

		self.last_update = now_datetime()

	def _parse_line(self, raw_line: str):
		escaped_line = ansi_escape(raw_line).strip()
		if not escaped_line:
			return

		self.lines.append(escaped_line)

		# Separate step index from line
		split = self._get_step_index_split(escaped_line)
		line = split["line"]

		# Final stage of the build
		if line.startswith("writing image"):
			self._set_docker_image_id(line)

		# Updates build step properties
		elif split["index"] in self.steps:
			self._update_dc_build_step(split)

		# Sets build step to running and adds it to self.steps
		else:
			self._add_step_to_steps_dict(split)

	def _end_parsing(self):
		if self.is_remote:
			self.dc.last_updated = now_datetime()

		self.dc.build_output = "".join(self.lines)
		self.dc.save()
		frappe.db.commit()

	def _set_docker_image_id(self, line: str):
		self.dc.docker_image_id = line.split()[2].split(":")[1]

	def _update_dc_build_step(self, split: "IndexSplit"):
		dc_step = self.dc_steps_by_index.get(split["index"])
		if not dc_step:
			return

		line = split["line"]
		if split["is_unusual"]:
			dc_step.output += line + "\n"
		elif line.startswith("sha256:"):
			dc_step.hash = line[7:]
		elif line.startswith("DONE"):
			dc_step.status = "Success"
			dc_step.duration = float(line.split()[1][:-1])
		elif line == "CACHED":
			dc_step.status = "Success"
			dc_step.cached = True
		elif line.startswith("ERROR"):
			dc_step.status = "Failure"
			dc_step.output += line[7:] + "\n"
		else:
			_, _, output = line.partition(" ")
			dc_step.output += output + "\n"

	def _add_step_to_steps_dict(self, split: "IndexSplit"):
		line = split["line"]
		if not line.startswith("[stage-"):
			return

		name = line.split("]", maxsplit=1)[1].strip()
		if not name.startswith("RUN"):
			return

		if not (match := re.search("`#stage-(.*)`", name)):
			return

		name, stage_slug, step_slug = get_name_and_slugs(name, match)
		dc_step = self.dc_steps_by_step_slug.get(step_slug)
		if not dc_step:
			return

		index = split["index"]
		dc_step.step_index = index
		dc_step.command = name
		dc_step.status = "Running"
		dc_step.output = ""

		if stage_slug == "apps":
			dc_step.command = f"bench get-app {step_slug}"

		self.steps[index] = dc_step

	def _get_step_index_split(self, escaped_line: str) -> "IndexSplit":
		splits = escaped_line.split(maxsplit=1)
		if len(splits) != 2:
			index = (sorted(self.steps)[-1],)
			return dict(index=index, line=escaped_line, is_unusual=True)

		index_str, line = splits
		try:
			index = int(index_str[1:])
			return dict(index=index, line=line, is_unusual=False)
		except ValueError:
			index = (sorted(self.steps)[-1],)
			return dict(index=index, line=escaped_line, is_unusual=True)


def ansi_escape(text: str) -> str:
	return ansi_escape_rx.sub("", text)


def get_name_and_slugs(name: str, match: "Match[str]") -> tuple[str, str, str]:
	if flags := dockerfile.parse_string(name)[0].flags:
		name = name.replace(flags[0], "")

	old = match.group(0)
	name = name.replace(old, "")
	name = name.strip()
	name = name.replace("   ", " \\\n  ")[4:]

	stage_slug, step_slug = match.group(1).split("-", maxsplit=1)
	return name, stage_slug, step_slug
