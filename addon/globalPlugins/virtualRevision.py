# Virtual Revision NVDA plugin
# Copyright (C) 2012-2020 Rui Batista and contributors
# Copyright (C) 2021-2023 Rui Fontes, Rui Batista and contributors
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

import globalPluginHandler
import globalVars
import api
import textInfos
import ui
import scriptHandler
import addonHandler
import config
import gui
import wx
from gui import guiHelper, settingsDialogs

addonHandler.initTranslation()

try:
	from globalCommands import SCRCAT_TEXTREVIEW
except ImportError:
	SCRCAT_TEXTREVIEW = None


def _isTermControl(obj):
	# True when obj is a Windows Terminal "TermControl" (the UIA element that hosts the text
	# of cmd/PowerShell/WSL tabs and panes). Reading UIAElement.currentClassName can raise a
	# COM error on some objects, and this runs against every object we inspect, so it must never
	# let an exception escape and abort the review command.
	try:
		return bool(getattr(obj, "UIAElement", None)) and obj.UIAElement.currentClassName == "TermControl"
	except Exception:
		return False


def _findTermControl(obj):
	# Return the focused object, or its nearest ancestor, that is a TermControl, else None.
	# NVDA normally puts focus directly on the TermControl, so this usually returns obj itself,
	# but walking up a few ancestors covers hosts that focus a child of the control. Capturing
	# straight from the focused control (rather than searching down from the foreground window)
	# is what makes every tab and split pane work, regardless of the host window's class name.
	node = obj
	depth = 0
	while node is not None and depth < 20:
		if _isTermControl(node):
			return node
		try:
			node = node.parent
		except Exception:
			break
		depth += 1
	return None


def _isDecorativeTerminalLine(line):
	# True for lines whose only non-whitespace characters are in the Unicode Box Drawing
	# block (U+2500-U+257F): the borders, separators, and corners that TUI apps paint.
	# Content lines that happen to contain a "│" border still survive because the
	# letters/digits inside fall outside the range.
	stripped = line.strip()
	if not stripped:
		return True
	return all("─" <= ch <= "╿" for ch in stripped)


def _cleanTerminalText(text):
	# Terminal cells are space-padded to the column width, and TUI apps reserve fixed
	# rows/columns for borders, status bars, input, etc. Captured as a UNIT_STORY this
	# turns into long runs of blank padding and box-border-only lines that bury the actual
	# content when the review window is navigated line by line. Strip per-line trailing
	# whitespace and drop lines that carry no real content.
	lines = []
	for line in text.split("\n"):
		stripped = line.rstrip()
		if _isDecorativeTerminalLine(stripped):
			continue
		lines.append(stripped)
	return "\n".join(lines)


config.conf.spec["virtualRevision"] = {
	"playBeep": "boolean(default=True)",
}


class VirtualRevisionSettingsPanel(settingsDialogs.SettingsPanel):
	# Translators: Title of the Virtual Revision settings panel.
	title = _("Virtual Revision")

	def makeSettings(self, settingsSizer):
		sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		# Translators: Label for the checkbox that toggles the alert beeps for UWP windows.
		self.playBeepCheckBox = sHelper.addItem(
			wx.CheckBox(self, label=_("Play alert &beeps when reviewing UWP windows")),
		)
		self.playBeepCheckBox.SetValue(config.conf["virtualRevision"]["playBeep"])

	def onSave(self):
		config.conf["virtualRevision"]["playBeep"] = self.playBeepCheckBox.GetValue()


def obtainUWPWindowText():
	foreground = api.getForegroundObject()
	desktop = api.getDesktopObject()
	uwpTextList = [foreground.name]
	curObject = foreground.firstChild
	while curObject:
		if curObject.name is not None:
			uwpTextList.append(curObject.name)
		if _isTermControl(curObject):
			info = curObject.makeTextInfo(textInfos.POSITION_ALL)
			text = _cleanTerminalText(info.clipboardText)
			uwpTextList.append(text)
		if curObject.simpleFirstChild:
			curObject = curObject.simpleFirstChild
			continue
		if curObject.simpleNext:
			curObject = curObject.simpleNext
			continue
		if curObject.simpleParent:
			parent = curObject.simpleParent
			# As long as one is on current foreground object...
			# Stay within the current top-level window.
			# Compare window handles rather than objects: an add-on that puts overlay classes on
			# the desktop object makes the identity comparison fail.
			grandParent = parent.simpleParent
			if grandParent is not None and grandParent.windowHandle == desktop.windowHandle:
				break
			while parent and not parent.simpleNext:
				parent = parent.simpleParent
			# But sometimes, the top-level window has no sibling at all (such is the case in Windows 10 Start menu).
			try:
				curObject = parent.simpleNext
			except AttributeError:
				continue
	if _isTermControl(foreground):
		info = foreground.makeTextInfo(textInfos.POSITION_ALL)
		text = _cleanTerminalText(info.clipboardText)
		uwpTextList.append(text)
	return uwpTextList


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	scriptCategory = SCRCAT_TEXTREVIEW

	def __init__(self):
		super().__init__()
		if not globalVars.appArgs.secure:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(VirtualRevisionSettingsPanel)

	def terminate(self):
		super().terminate()
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(VirtualRevisionSettingsPanel)
		except ValueError:
			pass

	@scriptHandler.script(
		# Translators: Message presented in input help mode.
		description=_("Opens a window containing the text of the currently focused window for easy review."),
		gestures=("kb:nvda+alt+w", "kb:nvda+control+w"),
	)
	def script_virtualWindowReview(self, gesture):
		# Find the first focus ancestor that have any display text, according to the display model
		# This must be the root application window, or something close to that.
		# In case of universal apps, traverse child elements.
		text = None
		obj = api.getFocusObject()
		# Modern command-line windows (Windows Terminal, and any XAML/UWP terminal host) expose
		# their text through a "TermControl" UIA element instead of the legacy console buffer.
		# Detect it from the focused control and capture straight from there, so every tab and
		# split pane works no matter what the host window's class name is. Checked first because
		# the terminal control's HWND class often matches the UWP prefixes below, and that branch's
		# generic child walk is slower and can miss the active pane.
		term = _findTermControl(obj)
		if term is not None:
			info = term.makeTextInfo(textInfos.POSITION_ALL)
			text = _cleanTerminalText(info.clipboardText)
		# Because it may take a while to iterate through elements, play abeep to alert users of this fact and the fact it's a UWP screen.
		elif obj.windowClassName.startswith(("Windows.UI.Core", "Windows.UI.Input.InputSite")):
			playBeep = config.conf["virtualRevision"]["playBeep"]
			if playBeep:
				import tones

				tones.beep(400, 300)
			text = "\n".join(obtainUWPWindowText())
			if playBeep:
				tones.beep(400, 50)
		else:
			root = None
			for ancestor in api.getFocusAncestors():
				if ancestor.appModule and ancestor.displayText:
					root = ancestor
					break
			if root:
				info = root.makeTextInfo(textInfos.POSITION_ALL)
				text = info.clipboardText.replace("\0", " ")
			if obj.windowClassName == "ConsoleWindowClass":
				info = obj.makeTextInfo(textInfos.POSITION_ALL)
				text = info.clipboardText.rstrip()
		if text:
			name = api.getForegroundObject().name
			if name in (None, ""):
				# Translators: The title of the virtual review window when the foreground window has no name, commonly seen when all windows are minimized.
				name = _("No title")
			# Translators: Title of the window shown for reading text on screen via a window.
			ui.browseableMessage(text, title=_("Virtual review: {screenName}").format(screenName=name))
		else:
			# Translator: Message shown when no text can be virtualized.
			ui.message(_("No text to display"))


# Avoid use on secure screens
if globalVars.appArgs.secure:
	# Override the global plugin to disable it.
	GlobalPlugin = globalPluginHandler.GlobalPlugin
