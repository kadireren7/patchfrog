import os
import sys


def run_command(user_input):
    try:
        return eval(user_input)
    except:
        pass


class Handler:
    def process(self, data):
        unused_local = 42
        return data
