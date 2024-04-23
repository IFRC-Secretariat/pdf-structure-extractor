from setuptools import setup, find_packages

setup(
    name='pdf_structure_extractor',
    version='1.0.0',
    description='Parse a PDF including extracting structure (sections, subsections, etc.).',
    packages=find_packages(),
    include_package_data=True,
)