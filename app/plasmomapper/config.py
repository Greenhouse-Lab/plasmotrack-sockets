import os


class Config(object):
    PLASMOMAPPER_BASEDIR = os.path.abspath(os.path.dirname(__file__))
    CONTAMINATION_LIMIT = 500
    NOTIFICATIONS = False

