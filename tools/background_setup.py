"""Configure automatic background startup or open/stop bridge diagnostics."""
import argparse
import json
from companion.background import BASE, install_startup, request, start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enable','disable','show','stop','status'])
    args = parser.parse_args();state = BASE/'state'
    if args.action == 'enable':
        config = json.loads((state/'launcher.json').read_text(encoding='utf-8'))
        if not config.get('addon') or not config.get('project') or not (state/'capture.json').is_file():
            parser.error('Complete addon/project/capture setup first with Launch Companion.pyw --configure')
        print('Login startup enabled:', install_startup());start(state)
    elif args.action == 'disable':
        print('Login startup removed:', install_startup(False))
    elif args.action == 'status':
        print((state/'bridge-status.json').read_text(encoding='utf-8'))
    else:
        request(state, args.action)


if __name__ == '__main__': main()
