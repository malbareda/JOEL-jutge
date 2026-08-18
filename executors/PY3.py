from dmoj.executors.python_executor import PythonExecutor
import re


class Executor(PythonExecutor):
    command = 'python3'
    command_paths = ['python%s' % i for i in ['3.6', '3.5', '3.4', '3.3', '3.2', '3.1', '3']]
    test_program = "print(__import__('sys').stdin.read(), end='')"
    name = 'PY3'
    fsize = 60485  # Permet escriptura de fitxers

    def get_write_fs(self):
        print("WRITE_FS:", "FSIZE:", self.fsize)
        return super().get_write_fs() + [
            re.escape(self._dir) + r'/out/',
            '/outputfiles/'
        ]
