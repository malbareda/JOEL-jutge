import re

from dmoj.cptbox.handlers import ALLOW
from dmoj.cptbox.syscalls import sys_clock_nanosleep, sys_statx
from dmoj.executors.java_executor import JAVA_SANDBOX, JavaExecutor, JavacExecutor


class Executor(JavacExecutor):
    compiler = 'javac25'
    vm = 'java25'
    name = 'JAVA25'
    jvm_regex = r'java-25-|openjdk25'
    # JDK 24+ removed the SecurityManager (System.setSecurityManager throws), so neither agent can sandbox Java 25 the
    # way JAVA_SANDBOX_LEGACY does for Java 8/11/Kotlin. Instead it runs under cptbox, like Python: the newer agent
    # only does stream/encoding handling here, and get_security() below does the sandboxing.
    agent_file = JAVA_SANDBOX
    fs = [re.escape(JAVA_SANDBOX), '/inputfiles/']  # the JVM itself is under /usr/, already readable
    fsize = 60485  # same as PY3: file problems write their output to out/ or /outputfiles/

    # No need to create out/ (cptbox refuses mkdir): file problems get in/ and out/ as symlinks from their init.yml
    # (`symlinks: {out: /outputfiles}`), made by PlatformExecutorMixin.launch(). Pre-creating out/ made that fail.

    def get_write_fs(self):
        return super().get_write_fs() + [re.escape(self._dir) + r'/out/', '/outputfiles/']

    def get_security(self, launch_kwargs=None):
        # JavaExecutor.get_security() returns None (no cptbox); skip it to get the cptbox every other language uses.
        sec = super(JavaExecutor, self).get_security(launch_kwargs)
        # Syscalls JDK 25 needs that this (2020) cptbox doesn't know about. statx takes (dirfd, path) like fstatat,
        # so it goes through the same path check instead of being allowed outright.
        sec[sys_statx] = sec.check_file_access_at('statx')
        sec[sys_clock_nanosleep] = ALLOW
        return sec

    test_program = """\
import java.io.IOException;

interface IORunnable {
    public void run() throws IOException;
}

public class self_test {
    public static void run(IORunnable target) throws IOException {
        target.run();
    }

    public static void main(String[] args) throws IOException {
        run(() -> {
            byte[] buffer = new byte[4096];
            int read;
            while ((read = System.in.read(buffer)) >= 0)
                System.out.write(buffer, 0, read);
        });
    }
}"""


    def get_compile_args(self):
        return [self.get_compiler(), '-encoding', 'UTF-8', self._code]
        #return [self.get_compiler(), self._code]

