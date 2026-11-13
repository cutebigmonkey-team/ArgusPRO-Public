import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
spec=importlib.util.spec_from_file_location('installer',Path(__file__).with_name('install-app.py'))
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'runtime'
        self.root.mkdir()
        self.shell=Path(self.temp.name)/'shell.zip'
        with zipfile.ZipFile(self.shell,'w') as z:
            for name in installer.SHELL: z.writestr(name,b'\0asmtest' if name=='app.wasm' else b'test')
        self.checksum=self.shell.with_suffix('.zip.sha256')
        self.checksum.write_text(hashlib.sha256(self.shell.read_bytes()).hexdigest())

    def test_update_preserves_private_data_and_backs_up_old_shell(self):
        for name,value in {'private/users.sqlite3':b'accounts','media/existing':b'media','postgres/PG_VERSION':b'17',
                           'web/index.html':b'old shell'}.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
        installer.install(self.root,self.shell)
        self.assertEqual((self.root/'private/users.sqlite3').read_bytes(),b'accounts')
        self.assertEqual((self.root/'media/existing').read_bytes(),b'media')
        self.assertEqual((self.root/'postgres/PG_VERSION').read_bytes(),b'17')
        self.assertEqual(next((self.root/'private').glob('app-backup-*/web/index.html')).read_bytes(),b'old shell')
        self.assertFalse((self.root/'collectors').exists())
        self.assertTrue((self.root/'Manage-Users.cmd').exists())

    def test_corrupt_or_unexpected_shell_rejected_before_write(self):
        self.checksum.write_text('0'*64)
        with self.assertRaises(ValueError): installer.install(self.root,self.shell)
        self.assertFalse((self.root/'app').exists())
        with zipfile.ZipFile(self.shell,'a') as z: z.writestr('../unexpected',b'bad')
        self.checksum.write_text(hashlib.sha256(self.shell.read_bytes()).hexdigest())
        with self.assertRaises(ValueError): installer.install(self.root,self.shell)
        self.assertFalse((self.root/'app').exists())

if __name__=='__main__': unittest.main()
