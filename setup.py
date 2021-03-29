from setuptools import setup, find_packages
setup(
        name = "mailnex",
        version = "0.0",
        package_data = {
            '': ['README', 'USER.README', 'INSTALL', 'requirements.txt'],
        },
        author = "John O'Meara",
        description = "Email client in the style of mailx",
        url = "http://linsam.homelinux.com/mailnex",
        packages = ['mailnex'],
        install_requires = [
            "Pygments==2.7.4",
            "argparse==1.2.1",
            "blessings==1.6",
            "keyring==9.0",
            "prompt-toolkit==1.0.0",
            "pyuv==1.3.0",
            "pyxdg==0.26",
            "six==1.10.0",
            "wcwidth==0.1.6",
            "wsgiref==0.1.2",
            "python-magic==0.4.12",
            "python-dateutil==2.4.2",
            ],
        entry_points = {
                'console_scripts': [ 'mailnex = mailnex.mailnex:main' ],
            },
     )
