import logging
from os_helpers import load_helpers
load_helpers()

__version__ = "0.19.0rc31"
version = tuple(__version__.split('.'))
logging.getLogger(__name__).addHandler(logging.NullHandler())
