import setuptools

with open('README.rst') as f:
    readme = f.read()

setuptools.setup(
    name='mkepub',
    version='1.3',
    description='Simple minimalistic library for creating EPUB3 files',
    long_description=readme,
    url='https://github.com/Le_Roux-nard/mkepub/',
    author='anqxyr, Le_Roux-nard',
    author_email='anqxyr@gmail.com, contact@lerouxnard.fr',
    license='MIT',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.12'],
    packages=['mkepub'],
    package_data={'mkepub': ['templates/*']},
    install_requires=['jinja2', 'pillow'],
)
