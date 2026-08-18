from dmoj.executors.shell_executor import ShellExecutor


class Executor(ShellExecutor):
    ext = 'sh'
    name = 'BASH'
    command = 'bash'
    test_program = 'cat 1'

    def get_cmdline(self):
        print("bash?aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        return ['bash', self._code]
