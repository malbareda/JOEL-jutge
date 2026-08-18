from dmoj.executors.PYPY import Executor as PYPYExecutor


class Executor(PYPYExecutor):
    command = 'pypy3'
    test_program = "print(__import__('sys').stdin.read(), end='')"
    name = 'PYPY3'
    fsize = 1048576  # Permet escriptura de fitxers

    def get_write_fs(self):
        print("WRITE_FS:", result, "FSIZE:", self.fsize)
        return super().get_write_fs() + ['/outputfiles/']