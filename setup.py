from setuptools import setup, find_packages
setup(
        name = "mailnex",
        version = "0.1",
        package_data = {
            '': ['README', 'USER.README', 'INSTALL', 'requirements.txt'],
        },
        author = "John O'Meara",
        description = "Email client in the style of mailx",
        url = "http://linsam.homelinux.com/mailnex",
        packages = ['mailnex'],
        install_requires = [
            "Pygments==2.20.0",
            "blessings==1.7",
            "keyring==23.5",
            "prompt-toolkit==3.0.28",
            "uvloop==0.16.0",
            "pyxdg==0.27",
            "wcwidth==0.2.5",
            "python-magic==0.4.24",
            "python-dateutil==2.8.1",
            ],
        entry_points = {
                'console_scripts': [ 'mailnex = mailnex.mailnex:main' ],
            },
     )
