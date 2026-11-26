"""Download one controller toolchain and compile the existing FISCO driver."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

RELEASE = 'https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/v3.7.3/'
JDK = 'https://cdn.azul.com/zulu/bin/zulu17.62.17-ca-jdk17.0.17-linux_x64.tar.gz'
MAVEN = 'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.tar.gz'
MAVEN_SHA512 = 'a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b'
FISCO_SHA256 = '7c29bd2afe615a57af02a440fc4d665c3beec3cf7301ec4007cc07caf5b683f4'


def Hash(path, algorithm='sha256'):
    value = hashlib.new(algorithm)
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def Save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes((json.dumps(value, indent=2) + '\n').encode())
    temporary.replace(path)


def Download(url, destination):
    if not destination.is_file() or destination.stat().st_size == 0:
        print('Downloading ' + destination.name, flush=True)
        pending = destination.with_name(destination.name + '.part')
        request = urllib.request.Request(url, headers={'User-Agent': 'FISCO100-bootstrap',
                                                      'Cache-Control': 'no-cache'})
        last = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as source, pending.open('wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                pending.replace(destination)
                last = None
                break
            except Exception as error:
                last = error
        if last:
            raise last
    return {'path': str(destination), 'url': url, 'bytes': destination.stat().st_size,
            'sha256': Hash(destination)}


def Extract(archive, directory):
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (
                    member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('Unexpected archive member: ' + member.name)
            if member.issym() or member.islnk():
                target = posixpath.normpath(posixpath.join(
                    str(path.parent) if member.issym() else '', member.linkname))
                if target.startswith('/') or target == '..' or target.startswith('../'):
                    raise ValueError('Archive link escapes its directory')
        if hasattr(tarfile, 'data_filter'):
            source.extractall(directory, filter='data')
        else:
            source.extractall(directory)


def InstallTool(archive, destination, executable):
    if (destination / 'bin' / executable).is_file():
        return
    with tempfile.TemporaryDirectory(prefix='.tool-', dir=destination.parent) as temporary:
        temporary = Path(temporary)
        Extract(archive, temporary)
        matches = list(temporary.glob('*/bin/' + executable))
        if len(matches) != 1 or destination.exists():
            raise ValueError('Expected one fresh ' + executable + ' toolchain directory')
        matches[0].parent.parent.rename(destination)


def Run(command, log, environment=None):
    print('Running ' + Path(command[0]).name + '; log: ' + str(log), flush=True)
    with log.open('w', encoding='utf-8') as output:
        subprocess.run([str(value) for value in command], env=environment, stdout=output,
                       stderr=subprocess.STDOUT, check=True, timeout=1200)


def CopyDriver(driver):
    """Reuse the existing Maven configuration and upstream contract wrappers."""
    fleet = Path(__file__).resolve().parent
    fisco = fleet.parent
    assets = {
        fleet / 'driver/NativeAlignedBench.java': driver / 'NativeAlignedBench.java',
        fisco / 'pom.xml': driver / 'pom.xml',
        fisco / 'Aligned/sdk-log4j2.xml': driver / 'sdk-log4j2.xml'}
    for name in ('DagTransfer.java', 'ParallelOk.java'):
        assets[fisco / 'vendor' / name] = driver / 'src/org/fisco/bcos/sdk/demo/contract' / name
    for source, destination in assets.items():
        if not source.is_file():
            raise ValueError('Missing repository driver asset: ' + str(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)


def Main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/fisco-fleet'))
    parser.add_argument('--fisco-url', default=RELEASE + 'fisco-bcos-linux-x86_64.tar.gz')
    parser.add_argument('--jdk-url', default=JDK)
    parser.add_argument('--jdk-sha256')
    parser.add_argument('--maven-url', default=MAVEN)
    parser.add_argument('--maven-mirror', default='https://maven.aliyun.com/repository/public')
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--driver-only', action='store_true', help='Prepare JDK/SDK while a separate node download is running')
    args = parser.parse_args(argv)
    root = args.root.absolute()
    if root == Path(root.anchor):
        parser.error('root must be a directory below a filesystem root')
    root.mkdir(parents=True, exist_ok=True)
    for name in ('downloads', 'bin', 'tools', 'toolchain', 'logs', 'driver/classes', 'driver/lib'):
        (root / name).mkdir(parents=True, exist_ok=True)
    record = {'status': 'running', 'started_unix': time.time(), 'root': str(root)}
    Save(root / 'prepare.json', record)
    try:
        downloads = {
            'jdk': (args.jdk_url, root / 'downloads/jdk17-linux-x64.tar.gz'),
            'maven': (args.maven_url, root / 'downloads/apache-maven-3.9.9.tar.gz')}
        if not args.driver_only:
            downloads.update(fisco=(args.fisco_url, root / 'downloads/fisco-bcos-linux-x86_64.tar.gz'),
                             builder=(RELEASE + 'build_chain.sh', root / 'downloads/build_chain.sh'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(Download, url, path)
                       for name, (url, path) in downloads.items()}
            record['downloads'] = {name: future.result() for name, future in futures.items()}
        if Hash(downloads['maven'][1], 'sha512') != MAVEN_SHA512:
            raise ValueError('Maven archive differs from its official SHA512')
        if args.jdk_sha256 and Hash(downloads['jdk'][1]) != args.jdk_sha256:
            raise ValueError('JDK archive differs from the requested SHA256')
        if args.download_only:
            record['status'] = 'downloaded'
            Save(root / 'prepare.json', record)
            return 0
        if sys.platform != 'linux':
            raise ValueError('Compilation requires Linux; use --download-only on another OS')
        if not args.driver_only:
            node = root / 'bin/fisco-bcos'
            if not node.is_file():
                with tempfile.TemporaryDirectory(prefix='.fisco-', dir=root) as temporary:
                    temporary = Path(temporary)
                    Extract(downloads['fisco'][1], temporary)
                    matches = [path for path in temporary.rglob('fisco-bcos') if path.is_file()]
                    if len(matches) != 1:
                        raise ValueError('Expected one fisco-bcos binary')
                    shutil.copyfile(matches[0], node)
            if Hash(node) != FISCO_SHA256:
                raise ValueError('FISCO binary differs from the previously verified official v3.7.3 binary')
            node.chmod(0o755)
            shutil.copyfile(downloads['builder'][1], root / 'tools/build_chain.sh')
            (root / 'tools/build_chain.sh').chmod(0o755)
        InstallTool(downloads['jdk'][1], root / 'toolchain/jdk', 'javac')
        InstallTool(downloads['maven'][1], root / 'toolchain/maven', 'mvn')
        java, javac = root / 'toolchain/jdk/bin/java', root / 'toolchain/jdk/bin/javac'
        environment = dict(os.environ, JAVA_HOME=str(java.parent.parent),
                           MAVEN_OPTS='-Xmx768m', LC_ALL='C')
        environment['PATH'] = str(java.parent) + os.pathsep + environment.get('PATH', '')
        version = subprocess.check_output([str(java), '-version'], stderr=subprocess.STDOUT,
                                          text=True, env=environment, timeout=30)
        if 'version "17.' not in version:
            raise ValueError('Expected JDK 17')
        record['java_version'] = version
        if not args.driver_only:
            Run([node, '--version'], root / 'logs/fisco-version.log', environment)
            link = subprocess.run(['ldd', str(node)], capture_output=True, text=True,
                                  env=environment, timeout=30)
            text = link.stdout + link.stderr
            (root / 'logs/fisco-ldd.log').write_text(text)
            if ('not found' in text or
                    (link.returncode and 'not a dynamic executable' not in text and
                     'statically linked' not in text)):
                raise ValueError('FISCO runtime libraries are missing')
        driver = root / 'driver'
        CopyDriver(driver)
        settings = root / 'maven-settings.xml'
        from xml.sax.saxutils import escape
        settings.write_bytes(('<settings><mirrors><mirror><id>bootstrap</id><mirrorOf>*</mirrorOf>'
                              '<url>' + escape(args.maven_mirror) +
                              '</url></mirror></mirrors></settings>\n').encode())
        Run([root / 'toolchain/maven/bin/mvn', '-B', '-ntp', '-s', settings, '-f', driver / 'pom.xml',
             '-Dmaven.repo.local=' + str(root / 'maven-repository'),
             'org.apache.maven.plugins:maven-dependency-plugin:3.6.1:copy-dependencies',
             '-DoutputDirectory=' + str(driver / 'lib')], root / 'logs/maven.log', environment)
        sources = [driver / 'src/org/fisco/bcos/sdk/demo/contract' / name
                   for name in ('DagTransfer.java', 'ParallelOk.java')]
        sources.append(driver / 'NativeAlignedBench.java')
        Run([javac, '-J-Xmx512m', '-proc:none', '--release', '17',
             '-cp', str(driver / 'lib/*'), '-d', driver / 'classes', *sources],
            root / 'logs/javac.log', environment)
        record.update(status='driver_ready' if args.driver_only else 'complete', finished_unix=time.time(),
                      java_source_sha256=Hash(driver / 'NativeAlignedBench.java'),
                      java_class_sha256=Hash(driver / 'classes/NativeAlignedBench.class'),
                      jars=len(list((driver / 'lib').glob('*.jar'))))
        if not args.driver_only:
            record['node_sha256'] = Hash(node)
        Save(root / 'prepare.json', record)
        print(json.dumps(record), flush=True)
        return 0
    except Exception as error:
        record.update(status='failed', error=str(error), finished_unix=time.time())
        Save(root / 'prepare.json', record)
        raise


if __name__ == '__main__':
    raise SystemExit(Main())
