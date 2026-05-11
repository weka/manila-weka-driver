# Stub — provides minimal oslo_log.log for standalone testing.
import logging


def getLogger(name):
    return logging.getLogger(name)
