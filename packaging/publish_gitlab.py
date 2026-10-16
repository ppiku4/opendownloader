"""Publish only CI-built files using GitLab's short-lived job token."""
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def publish():
    api = os.environ['CI_API_V4_URL']
    project = os.environ['CI_PROJECT_ID']
    tag = os.environ['CI_COMMIT_TAG']
    token = os.environ['CI_JOB_TOKEN']
    base = f'{api}/projects/{project}'
    links = []
    for file in sorted(Path('release').iterdir()):
        if not file.is_file():
            continue
        url = f'{base}/packages/generic/opendownloader/{quote(tag, safe="")}/{quote(file.name, safe="")}'
        request = Request(url, data=file.read_bytes(), method='PUT',
                          headers={'JOB-TOKEN': token, 'Content-Type': 'application/octet-stream'})
        with urlopen(request, timeout=600) as response:
            if response.status != 201:
                raise RuntimeError(f'Package upload failed: {file.name}')
        links.append({'name': file.name, 'url': url, 'direct_asset_path': '/' + file.name})
    notes = Path('packaging/release-notes.md').read_text(encoding='utf-8')
    notes = notes.replace('{VERSION}', tag.removeprefix('v'))
    notes = notes.replace('Built by GitHub Actions', 'Built by GitLab CI/CD')
    notes = notes.replace('Already installed? Use *Help → Check for updates…* in OpenDownloader.',
                          'The in-app updater currently checks GitHub. Install this GitLab release from the downloads above.')
    notes = ('## Changes\n\n'
             '- Add a tag-triggered GitLab Windows build and release pipeline.\n'
             '- Test before building and publish CI-built packages with SHA-256 checksums.\n\n' + notes)
    notes = notes.replace('## Changes\n\n', '## Changes\n\n- Report active connections immediately after splitting downloads.\n')
    body = {'name': f'OpenDownloader {tag.removeprefix("v")}', 'tag_name': tag,
            'description': notes, 'assets': {'links': links}}
    request = Request(f'{base}/releases', data=json.dumps(body).encode(), method='POST',
                      headers={'JOB-TOKEN': token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    print('Published:', result['_links']['self'])


if __name__ == '__main__':
    publish()
