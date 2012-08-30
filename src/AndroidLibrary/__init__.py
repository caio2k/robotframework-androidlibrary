import json
import logging
import os
import subprocess
import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
execfile(os.path.join(THIS_DIR, 'version.py'))

__version__ = VERSION

import robot
from robot.variables import GLOBAL_VARIABLES
from robot.api import logger

import killableprocess
import tempfile

class AndroidLibrary(object):

    ROBOT_LIBRARY_VERSION = VERSION
    ROBOT_LIBRARY_SCOPE = 'GLOBAL'

    def __init__(self, ANDROID_HOME=None):
        '''
        Path to the Android SDK. Optional if the $ANDROID_HOME environment variable is set.
        '''

        if ANDROID_HOME is None:
            ANDROID_HOME = os.environ['ANDROID_HOME']

        self._ANDROID_HOME = ANDROID_HOME
        self._screenshot_index = 0

        self._adb = self._sdk_path(['platform-tools/adb', 'platform-tools/adb.exe'])
        self._emulator = self._sdk_path(['tools/emulator', 'tools/emulator.exe'])

    def _sdk_path(self, paths):
        for path in paths:
            complete_path = os.path.abspath(os.path.join(self._ANDROID_HOME, path))
            if os.path.exists(complete_path):
                return complete_path

        raise AssertionError("Couldn't find %s binary in %s" % (
          os.path.splitext(os.path.split(complete_path)[1])[0],
          os.path.split(complete_path)[0],
        ))

    def start_emulator(self, avd_name, no_window=False):
        '''
        Starts the Android Emulator.

        `avd_name` Identifier of the Android Virtual Device, for valid values on your machine run "$ANDROID_HOME/tools/android list avd|grep Name`
        `no_window` Set to True to start the emulator without GUI, useful for headless environments.
        '''
        args = [self._emulator, '-avd', avd_name]

        if no_window:
            args.append('-no-window')

        logging.debug("$> %s", ' '.join(args))

        self._emulator_proc = subprocess.Popen(args)

    def stop_emulator(self):
        '''
        Halts a previously started Android Emulator.
        '''

        if not hasattr(self, '_emulator_proc'):
            logging.warn("Could not stop Android Emulator: It was not started.")
            return

        self._emulator_proc.terminate()
        self._emulator_proc.kill()
        self._emulator_proc.wait()

        self._emulator_proc = None


    def _execute_with_timeout(self, cmd, max_attempts=3, max_timeout=120):
        logging.debug("$> %s # with timeout %ds", ' '.join(cmd), max_timeout)

        attempt = 0

        while attempt < max_attempts:
            attempt = attempt + 1
            out = tempfile.NamedTemporaryFile(delete=False)
            err = tempfile.NamedTemporaryFile(delete=False)
            p = killableprocess.Popen(cmd, stdout=out, stderr=err)
            p.wait(max_timeout)
            out.flush()
            out.close()
            err.flush()
            err.close()

            # -9 and 127 are returned by killableprocess when a timeout happens
            if  p.returncode == -9 or p.returncode == 127:
                logging.warn("Executing %s failed executing in less then %d seconds and was killed, attempt number %d of %d" % (
                    ' '.join(cmd), max_timeout, attempt, max_attempts))
                continue

        try:
            outfile = open(out.name, 'r')
            errfile = open(err.name, 'r')
            return p.returncode, outfile.read(), errfile.read()
        finally:
            outfile.close()
            os.unlink(out.name)
            errfile.close()
            os.unlink(errfile.name)

    def _wait_for_package_manager(self):
        attempts = 0
        max_attempts = 3

        while attempts < max_attempts:
            rc, output, errput = self._execute_with_timeout([
                self._adb, "wait-for-device", "shell", "pm", "path", "android"
              ], max_timeout=60, max_attempts=3)
            assert rc == 0, "Waiting for package manager failed: %d, %r, %r" % (rc, output, errput)

            if not 'Could not access the Package Manager.' in output:
                return

        raise AssertionError(output)

    def uninstall_application(self, package_name):
        self._wait_for_package_manager()

        rc, output, errput = self._execute_with_timeout([self._adb, "uninstall", package_name])
        assert rc == 0, "Uninstalling application failed: %d, %r" % (rc, output)
        assert output != None
        logging.debug(output)
        assert 'Error' not in output, output

    def install_application(self, apk_file):
        '''
        Installs the given Android application package file (APK) on the emulator along with the test server.

        For instrumentation (and thus all remote keywords to work) both .apk
        files must be signed with the same key.

        `apk_file` Path the the application to install
        '''

        self._wait_for_package_manager()

        rc, output, errput = self._execute_with_timeout([self._adb, "install", "-r", apk_file])
        logging.debug(output)
        assert rc == 0, "Installing application failed: %d, %r" % (rc, output)
        assert output != None
        assert 'Error' not in output, output

    def wait_for_device(self, timeout=120):
        '''
        Wait for the device to become available
        '''
        rc, output, errput = self._execute_with_timeout([self._adb, 'wait-for-device'], max_timeout=timeout/3, max_attempts=3)
        assert rc == 0, "wait for device application failed: %d, %r" % (rc, output)

    def send_key(self, key_code):
        '''
        Send key event with the given key code. See http://developer.android.com/reference/android/view/KeyEvent.html for a list of available key codes.

        `key_code` The key code to send
        '''
        rc, output, errput = self._execute_with_timeout([self._adb, 'shell', 'input', 'keyevent', '%d' % key_code])
        assert rc == 0

    def press_menu_button(self):
        '''
        Press the menu button ("KEYCODE_MENU"), same as '| Send Key | 82 |'
        '''
        self.send_key(82)

    def start_testserver(self, package_name):
        '''
        Start the remote test server inside the Android Application.

        `package_name` fully qualified name of the application to test

        '''
        rc, output, errput = self._execute_with_timeout([
          self._adb,
          "wait-for-device",
          "forward",
          "tcp:%d" % 34777,
          "tcp:7102"
        ])

        args = [
          self._adb,
          "wait-for-device",
          "shell",
          "am",
          "instrument",
          "-w",
          "-e",
          "class",
          "sh.calaba.instrumentationbackend.InstrumentationBackend",
          "%s.test/sh.calaba.instrumentationbackend.CalabashInstrumentationTestRunner" % package_name,
        ]

        self._host = 'localhost'
        self._port = 34777
        self._url = 'http://%s:%d' % (self._host, self._port)

        logging.debug("$> %s", ' '.join(args))
        self._testserver_proc = subprocess.Popen(args)

    def stop_testserver(self):
        '''
        Halts a previously started Android Emulator.
        '''
        response = requests.get(self._url + '/kill')

        assert response.status_code == 200, "InstrumentationBackend sent status %d, expected 200" % response.status_code
        assert response.text == 'Affirmative!', "InstrumentationBackend replied '%s', expected 'pong'" % response.text

    def connect_to_testserver(self):
        '''
        Connect to the previously started test server inside the Android
        Application. Performs a handshake.
        '''

        response = requests.get(self._url + '/ping')

        assert response.status_code == 200, "InstrumentationBackend sent status %d, expected 200" % response.status_code
        assert response.text == 'pong', "InstrumentationBackend replied '%s', expected 'pong'" % response.text

    def _perform_action(self, command, *arguments):
        action = json.dumps({
          "command": command,
          "arguments": arguments,
        })

        logging.debug(">> %r", action)

        response = requests.post(self._url,
          data = { 'command': action },
          headers = { 'Content-Type': 'application/x-www-form-urlencoded' },
        )

        logging.debug("<< %r", response.text)
        assert response.status_code == 200, "InstrumentationBackend sent status %d, expected 200" % response.status_code
        return response.json

    # BEGIN: STOLEN FROM SELENIUM2LIBRARY

    def _get_log_dir(self):
        logfile = GLOBAL_VARIABLES['${LOG FILE}']
        if logfile != 'NONE':
            return os.path.dirname(logfile)
        return GLOBAL_VARIABLES['${OUTPUTDIR}']

    def _get_screenshot_paths(self, filename):
        if not filename:
            self._screenshot_index += 1
            filename = 'android-screenshot-%d.png' % self._screenshot_index
        else:
            filename = filename.replace('/', os.sep)
        logdir = self._get_log_dir()
        path = os.path.join(logdir, filename)
        link = robot.utils.get_link_path(path, logdir)
        return path, link

    # END: STOLEN FROM SELENIUM2LIBRARY

    def capture_screenshot(self, filename=None):
        '''
        Captures a screenshot of the current screen and embeds it in the test report

        Also works in headless environments.

        `filename` Location where the screenshot will be saved.
        '''

        path, link = self._get_screenshot_paths(filename)
        response = requests.get(self._url + '/screenshot')

        with open(path, 'w') as f:
            f.write(response.content)
            f.close()

        assert response.status_code == 200, "InstrumentationBackend sent status %d, expected 200" % response.status_code

        logger.info('</td></tr><tr><td colspan="3"><a href="%s">'
                   '<img src="%s"></a>' % (link, link), True, False)

    def screen_should_contain(self, text):
        '''
        Asserts that the current screen contains a given text

        `text` String that should be on the current screen
        '''
        result = self._perform_action("assert_text", text, True)
        assert result["success"] == True, "Screen does not contain text '%s': %s" % (
                text, result.get('message', 'No specific error message given'))

    def screen_should_not_contain(self, text):
        '''
        Asserts that the current screen does not contain a given text

        `text` String that should not be on the current screen
        '''
        result = self._perform_action("assert_text", text, False)
        assert result["success"] == True, "Screen does contain text '%s', but shouldn't have: %s" % (
                text, result.get('message', 'No specific error message given'))

    def touch_button(self, text):
        '''
        Touch an android.widget.Button

        `text` is the text the button that will be clicked contains
        '''
        result = self._perform_action("press_button_with_text", text)
        assert result["success"] == True, "Touching button failed '%s': %s" % (
                text, result.get('message', 'No specific error message given'))

    def touch_text(self, text):
        '''
        Touch a text that is present on the screen

        `text` is the text the button that will be clicked contains
        '''
        result = self._perform_action("click_on_text", text)
        assert result["success"] == True, "Touching text '%s' failed: %s" % (
                text, result.get('message', 'No specific error message given'))

    def scroll_up(self):
        '''
        Scroll up
        '''
        result = self._perform_action("scroll_up")
        assert result["success"] == True, "Scrolling up failed '%s': %s" % (
                text, result.get('message', 'No specific error message given'))

    def scroll_down(self):
        '''
        Scroll down
        '''
        result = self._perform_action("scroll_down")
        assert result["success"] == True, "Scrolling down failed '%s': %s" % (
                text, result.get('message', 'No specific error message given'))

    def set_webview_text(self, locator, value):
        '''
        Set the <input> field in the webview to the given value

        `locator` the locator to find the element to change. Valid locators are in the form of css=#element_id or xpath=//input[0]
        `value` the new value
        '''

        try:
            strategy, query = locator.split("=")
        except ValueError, e:
            strategy = "css"
            query = locator

        result = self._perform_action("set_text", strategy, query, value)

        assert result["success"] == True, "Setting webview text failed '%r'" % result

    def touch_webview_element(self, locator):
        '''
        Touch an element in a webview

        `locator` locator for element to trigger a click event (only css locators are supported at the moment)
        '''

        try:
            strategy, query = locator.split("=")
        except ValueError, e:
            strategy = "css"
            query = locator

        assert strategy == "css"

        result = self._perform_action("touch", strategy, query)
        assert result["success"] == True, "Touching Webview element failed: '%r'" % result

    def set_text(self, locator, value):
        '''
        Set text in a native text field.

        See `Set Webview Text` to set the text in an input element in an embedded webview.

        `locator` which text field to set. Valid locators are '<int>' or 'num=<int>' for a numbered text field, 'name=<'name=<string>' for a named text field

        `value` the new value of the native text field
        '''

        try:
            strategy, query = locator.split("=")
        except ValueError, e:
            strategy = "num"
            query = locator

            logging.debug("No explicit locator strategy set, using '%s'" % strategy)

        if strategy in ("num", ):
            try:
                query = int(query, 10)
            except ValueError, e:
                raise AssertionError("Could not convert '%s' to integer, but required for '%s' locator strategy" % (
                  query, strategy
                ))

        api_names = {
          'num':  'enter_text_into_numbered_field',
          'name': 'enter_text_into_named_field',
        }

        assert strategy in api_names.keys(), 'Locator strategy must be one of "%s", but was %s' % (
          '", "'.join(api_names.keys()), strategy
        )

        result = self._perform_action(api_names[strategy], value, query)

        assert result["success"] == True, "Setting the text failed: %s" % result

    def swipe_left(self):
        '''
        Performs a swipe gesture to the left
        '''
        result = self._perform_action('swipe', 'left')

        assert result["success"] == True, "Swiping left failed: %s" % result

    def swipe_right(self):
        '''
        Performs a swipe gesture to the right
        '''
        result = self._perform_action('swipe', 'right')
        assert result["success"] == True, "Swiping right failed: %s" % result

