# Stub — provides minimal oslo_concurrency.processutils for standalone testing.


class ProcessExecutionError(Exception):
    def __init__(self, stdout='', stderr='', exit_code=None, cmd=None,
                 description=None):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.cmd = cmd
        self.description = description
        super(ProcessExecutionError, self).__init__(
            stderr or description or 'process execution error'
        )


def execute(*cmd, **kwargs):
    raise NotImplementedError("stub: processutils.execute not available")
