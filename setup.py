from setuptools import setup, find_packages

setup(
    name="rcar_flash",
    version="0.1.0",
    description="Tool for flashing R-Car boards",
    url='https://github.com/xen-troops/rcar_flash',
    author='Volodymyr Babchuk',
    author_email='volodymyr_babchuk@epam.com',
    packages=find_packages(),
    package_data={
        'rcar_flash': [
        'rcar_flash.yaml',
        'mot/*',
        ],
    },

    install_requires=[
        'pyserial',
        'PyYAML',
        'pyftdi',
    ],
    entry_points={
        'console_scripts': [
            'rcar_flash=rcar_flash.rcar_flash:main',
        ],
    },
    python_requires='>=3.9',
)
