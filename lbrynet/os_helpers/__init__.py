import sys
import os


def load_helpers():
    if 'ANDROID_ARGUMENT' in os.environ:
        import android
    elif 'win' in sys.platform:
        # these need to be imported in order
        import pywintypes  # pylint: disable=import-error
        import pythoncom  # pylint: disable=import-error
        import win32api  # pylint: disable=import-error
        import windows
    elif 'darwin' in sys.platform:
        pass
    else:  # linux
        pass
