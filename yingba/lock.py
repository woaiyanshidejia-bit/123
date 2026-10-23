"""Kernel-managed process lock: released automatically after a forced kill."""
import os
class InstanceLock:
    def __init__(self,path):self.path=path;self.file=None
    def __enter__(self):
        self.file=open(self.path,'a+b');self.file.seek(0)
        if os.path.getsize(self.path)==0:self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise RuntimeError('工作台已在运行，请使用已打开的浏览器窗口')
        return self
    def __exit__(self,*args):self.file.close()
