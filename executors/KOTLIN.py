import glob
import os.path
import zipfile

from dmoj.error import CompileError
from dmoj.executors.java_executor import JavaExecutor

with open(os.path.join(os.path.dirname(__file__), 'java-security.policy')) as policy_file:
    policy = policy_file.read()


class Executor(JavaExecutor):
    name = 'KOTLIN'
    ext = 'kt'

    compiler = 'kotlinc'
    compiler_time_limit = 40
    vm = 'kotlin_vm'
    stdlib = 'kotlin_stdlib'
    security_policy = policy

    test_program = '''\
fun main(args: Array<String>) {
    println(readLine())
}
'''

    def create_files(self, problem_id, source_code, *args, **kwargs):
        super().create_files(problem_id, source_code, *args, **kwargs)
        self._jar_name = '%s.jar' % problem_id

    def get_compiled_file(self):
        # Called from CompiledExecutor.compile() right after a successful compile. Without
        # -include-runtime (see get_compile_args), the jar only contains the student's own classes,
        # so it's run via -cp + an explicit main class instead of -jar (which would need the runtime
        # bundled in). kotlinc always writes the correct Main-Class into the jar's manifest on its
        # own, so read it back rather than re-deriving Kotlin's file-to-class-name mangling ourselves.
        with zipfile.ZipFile(self._file(self._jar_name)) as jar:
            manifest = jar.read('META-INF/MANIFEST.MF').decode('utf-8')
        for line in manifest.splitlines():
            if line.startswith('Main-Class:'):
                self._main_class = line.split(':', 1)[1].strip()
                break
        else:
            raise CompileError('Could not find Main-Class in the compiled jar\'s manifest')
        return super().get_compiled_file()

    def get_cmdline(self):
        res = super().get_cmdline()
        res[-2:] = ['-cp', '%s:%s' % (self._jar_name, self.get_stdlib())]
        res.append(self._main_class)
        return res

    def get_compile_args(self):
        return [self.get_compiler(), '-d', self._jar_name, self._code]

    @classmethod
    def get_stdlib(cls):
        return cls.runtime_dict.get(cls.stdlib)

    @classmethod
    def get_versionable_commands(cls):
        return [('kotlinc', cls.get_compiler()), ('java', cls.get_vm())]

    @classmethod
    def initialize(cls):
        stdlib = cls.get_stdlib()
        if stdlib is None or not os.path.isfile(stdlib):
            return False
        return super().initialize()

    @classmethod
    def autoconfig(cls):
        kotlinc = cls.find_command_from_list(['kotlinc'])
        if kotlinc is None:
            return None, False, 'Failed to find "kotlinc"'

        java = cls.find_command_from_list(['java'])
        if java is None:
            return None, False, 'Failed to find "java"'

        # Not derived from the kotlinc path itself: on this box "kotlinc" is a snap command, whose
        # symlink chain resolves to the generic /usr/bin/snap dispatcher rather than the real
        # installation directory, so the usual "sibling lib/ of the compiler" trick doesn't work here.
        stdlib_candidates = (glob.glob('/snap/kotlin/current/lib/kotlin-stdlib.jar') +
                             glob.glob('/usr/share/kotlin*/lib/kotlin-stdlib.jar'))
        stdlib = stdlib_candidates[0] if stdlib_candidates else None
        if stdlib is None:
            return None, False, 'Failed to find kotlin-stdlib.jar'

        return cls.autoconfig_run_test(
            {cls.compiler: kotlinc, cls.vm: cls.unravel_java(java), cls.stdlib: stdlib},
        )
