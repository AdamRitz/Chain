"""Exercise Ansible run_once failure recovery with three local inventory hosts."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PLAYBOOK = shutil.which('ansible-playbook')
if PLAYBOOK is None and Path(sys.executable).with_name('ansible-playbook').is_file():
    PLAYBOOK = str(Path(sys.executable).with_name('ansible-playbook'))


@unittest.skipUnless(os.name == 'posix' and PLAYBOOK, 'Requires a Linux Ansible controller')
class BenchmarkCleanupTests(unittest.TestCase):
    def RunCase(self, returncode):
        import yaml

        source = Path(__file__).resolve().parents[1] / 'cluster_benchmark.yml'
        play = copy.deepcopy(yaml.safe_load(source.read_text())[-1])
        first = next(index for index, task in enumerate(play['tasks']) if 'block' in task)
        # Exercise the production benchmark block and every task after it. Only
        # external node work is replaced; run_once/error/recovery rules stay intact.
        play['tasks'] = play['tasks'][first:]
        play['become'] = False
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / 'results'
            results.mkdir()
            for name in ('scripts', 'templates', 'tasks'):
                (root / name).mkdir()
            (root / 'scripts/cluster_benchmark.py').write_text(
                f'import sys\nsys.exit({returncode})\n')
            (root / 'templates/cluster-benchmark.json.j2').write_text('{}\n')
            cleanup = [{'name': 'Record recovery for this host', 'ansible.builtin.copy': {
                'content': '{"ok":true}\n',
                'dest': '{{ chain_result_dir }}/cleanup-{{ inventory_hostname }}.json',
                'mode': '0600'}}]
            (root / 'tasks/cleanup_group.yml').write_text(yaml.safe_dump(cleanup))
            play['vars'] = {**play.get('vars', {}), 'chain_run_id': 'cleanup-test',
                            'chain_controller_python': sys.executable,
                            'chain_result_dir': str(results)}
            (root / 'playbook.yml').write_text(yaml.safe_dump([play], sort_keys=False))
            hosts = {'node001': {}, 'node002': {}, 'node003': {}}
            inventory = {'all': {'children': {'chain': {'hosts': hosts, 'vars': {
                'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable}}}}}
            (root / 'inventory.json').write_text(json.dumps(inventory))
            (root / 'ansible.cfg').write_text('[defaults]\nretry_files_enabled = False\n')
            environment = {**os.environ, 'ANSIBLE_CONFIG': str(root / 'ansible.cfg'),
                           'ANSIBLE_LOCAL_TEMP': str(root / 'local-tmp'),
                           'ANSIBLE_REMOTE_TEMP': str(root / 'remote-tmp'),
                           'ANSIBLE_STDOUT_CALLBACK': 'default', 'ANSIBLE_NOCOLOR': '1'}
            process = subprocess.run([PLAYBOOK, '-i', str(root / 'inventory.json'),
                                      str(root / 'playbook.yml'), '--forks', '3'],
                                     cwd=root, env=environment, capture_output=True,
                                     text=True, timeout=90)
            details = process.stdout + process.stderr
            self.assertEqual({path.name for path in results.glob('cleanup-*.json')},
                             {f'cleanup-{host}.json' for host in hosts}, details)
            if returncode:
                self.assertNotEqual(process.returncode, 0, details)
                self.assertIn('The benchmark failed', details)
            else:
                self.assertEqual(process.returncode, 0, details)

    def test_failed_benchmark_cleans_every_host_before_reporting_failure(self):
        self.RunCase(19)

    def test_successful_benchmark_cleans_every_host(self):
        self.RunCase(0)


if __name__ == '__main__':
    unittest.main()
