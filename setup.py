from setuptools import setup, find_packages

setup(
    name="refactor_utils",
    version="1.4.7",
    package_dir={"": "src"},  # This tells setuptools packages are under src/
    packages=find_packages(where="src"),
    entry_points={
        "console_scripts": [
            "pyrefactor = refactor_utils.cli:main",
        ],
    },
    python_requires=">=3.11",
    description="Refactor small python packages",
    author="Isaac Onagh",
    author_email="your.email@example.com",
    url="https://github.com/yourusername/pyrefactor",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
