from pathlib import Path
import importlib.util,tempfile,unittest
spec=importlib.util.spec_from_file_location('packer',Path(__file__).with_name('pack-runtime.py'))
packer=importlib.util.module_from_spec(spec);spec.loader.exec_module(packer)

class ArchiveScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.manifest={'module_files':{'a/index.js':{'kind':'file'}},'company_source_targets':{'/opt/personal-cloud/worker.mjs':'0'*64}}
        for name in ('candidate-manifest.json','opt/dsh/node_modules/a/index.js','opt/personal-cloud/worker.mjs'):
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
    def test_declared_tree(self):self.assertTrue(packer.declared_paths(self.root,self.manifest))
    def test_undeclared_root_content(self):
        p=self.root/'etc/entry';p.parent.mkdir();p.write_text('x')
        with self.assertRaisesRegex(ValueError,'UNDECLARED'):packer.declared_paths(self.root,self.manifest)
    def test_undeclared_company_code(self):
        (self.root/'opt/personal-cloud/evil.mjs').write_text('x')
        with self.assertRaisesRegex(ValueError,'UNDECLARED'):packer.declared_paths(self.root,self.manifest)
    def test_empty_npm_directory_allowed(self):
        (self.root/'opt/dsh/node_modules/empty').mkdir()
        self.assertTrue(packer.declared_paths(self.root,self.manifest))

if __name__=='__main__':unittest.main()
