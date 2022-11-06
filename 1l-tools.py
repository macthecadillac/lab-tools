#!/usr/bin/env python
# TODO: possibly create report in spreadsheet instead of making pdf
# TODO: add status to indicate how much a TA has finished for grade review in
# case they couldn't finish all grading by Monday
# TODO: group TA grade sheets by TA names instead of by section for easier
# access when there are 40 sections (breaking change)
# TODO: commandline option for output path
# TODO: pdf backend toggle
# TODO: avoid sequential if tectonic has been initialized
# TODO: choices for reset: full reset, reset only tectonic
# TODO: test everything on Windows (mainly for tectonic and configuration
# related stuff)
''' Assorted tools for 1-series lab administration '''

import abc
import argparse
import functools
import io
import itertools
import math
import matplotlib.style
import matplotlib.figure
import numpy
import openpyxl.styles
import os
import pandas
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import toml


class CodeGen:
    '''
    A class of code generator objects. The constructor of this class takes in
    Roster objects, the lab number, and a list of checkpoints for the lab to
    create generators that when run, will generate complete LaTeX document
    source code, which could then be compiled using pdfLaTeX.

    The CodeGen class satisfies all monoid laws (associativity, identity).
    Objects of this type can be added with the `+` operator and 'summed' over
    with the Python `sum` function.
    '''

    # LaTeX code preceding meaningful content
    header = [
        '\\documentclass[11pt]{article}',
        '\\usepackage[margin=0.85in]{geometry}',
        '\\usepackage{multirow}',
        '\\usepackage{tabularx}',
        '\\usepackage{makecell}',
        '\\usepackage[sfdefault, lf]{carlito}',
        '\\newcolumntype{?}{!{\\vrule width 2pt}}',
        '\\newcolumntype{C}{>{\\centering\\arraybackslash}X}',
        '\\setlength\\arrayrulewidth{1pt}',
        '\\renewcommand{\\arraystretch}{1.3}',
        '\\pagenumbering{gobble}',
        '\\begin{document}']

    footer = ['\\end{document}']

    def __init__(self, roster, lab, checkpoints):
        '''
        Initializer for CodeGen objects

        Parameters
        ----------
            roster : Roster
            lab : int
                The lab number
            checkpoints : list[str]
                A list of checkpoints
        '''
        self.lab = lab
        self.checkpoints = checkpoints
        self.code = dict()
        if roster is not None:
            self.section_codegen(roster)

    def __add__(self, other):
        ''' Set operation that satisfies the associative property '''
        code = {**self.code, **other.code}
        lab = self.lab if self.lab is not None else other.lab
        checkpoints = self.checkpoints if self.checkpoints is not None else other.checkpoints
        return CodeGen.from_code(code, lab, checkpoints)

    def __radd__(self, rhs):
        return self + rhs

    @classmethod
    def from_code(cls, code, lab, checkpoints):
        ''' Alternative constructor of the class from given LaTeX code '''
        obj = cls(None, lab, checkpoints)
        obj.code = code
        return obj

    @classmethod
    def mempty(cls):
        ''' Identity object '''
        return cls(None, None, None)

    def add_group(self, section, group, students):
        '''
        Generate code for a given group of students

        Parameters
        ----------
            section : int
                Section number
            group : int
                Group number
            students : list[str]
                List of student names
        '''
        if not students:
            return
        first_student = students[0]
        nrows = len(students)
        n = len(self.checkpoints)

        # the correct number of separators for this many checkpoints
        seps = '& ' * (n - 1)
        # LaTeX code to set up the first row of the group
        self.code[section].append('      & & \\multirow{{{}}}{{*}}'.format(nrows) +
                                  '{{\\textbf{{{}}}}} & '.format(group) +
                                  '{} & & {}\\\\'.format(first_student, seps))
        # LaTeX code for each subsequent row of the group
        for student in students[1:]:
            self.code[section].append('      \\cline{{1-2}}\\cline{{4-{}}}'.format(n + 5))
            self.code[section].append('      & & & {} & & {}\\\\'.format(student, seps))
        self.code[section].append('      \\hline')

    def section_codegen(self, roster):
        '''
        Generate code for one section

        Parameters
        ----------
            roster : Roster
        '''
        n = len(self.checkpoints)
        # LaTeX code setting up a table
        begin = [
            '\\begin{center}',
            '  \\begin{tabularx}{\\textwidth}{?>{\\hsize=5em}C|c|c|X|c|' +
            # adjust the table column setup based on the number of checkpoints
            '|'.join(['c'] * n) + '?}',
            '    \\Xhline{2pt}',
            '    \\multicolumn{{3}}{{?l}}{{\\textbf{{Lab {}}}}} & '.format(self.lab) +
            '\\multicolumn{{1}}{{C}}{{\\textbf{{Section {}}}}} '.format(roster.section) +
            '& \\multicolumn{{{}}}{{l?}}{{\\textbf{{Date:}}}} \\\\'.format(n + 1),
            '    \\Xhline{2pt}',
            '    \\textbf{Signature} & \\textbf{Late} & \\textbf{Group} & ' +
            '\\multicolumn{1}{c|}{\\textbf{Student}} & \\textbf{TA Check} & ' +
            # put in the checkpoints in the table column headings
            ' & '.join('\\textbf{{{}}}'.format(c).replace('&', '\&') for c in self.checkpoints) +
            ' \\\\',
            '    \\Xhline{2pt}']
        # LaTeX code at the end of a table
        end = [
            '    \\Xhline{2pt}',
            '  \\end{tabularx}',
            '\\end{center}',
            '\\newpage']
        section = roster.section
        self.code[section] = []
        for group, students in sorted(roster.groups.items()):
            self.add_group(section, group, students)
        # the last line is '\hline' which don't need
        self.code[section] = begin + self.code[section][:-1] + end

    def run(self):
        ''' Run the generator

        Returns
        ----------
            str: complete LaTeX source code for one document
        '''
        # concatenate code fragments of each section into a document body. The
        # last line is `\newpage` which we don't need
        code = sum((item[1] for item in sorted(self.code.items())), [])[:-1]
        # add in the front matter and auxiliary code to make a complete source
        # file
        return '\n'.join(self.header + code + self.footer)

    @property
    def section(self):
        if len(self.code) == 1:
            return list(self.code.keys())[0]
        else:
            raise ValueError('generator has multiple sections. fatal error. ' +
                             'check code')


class PDFWriter:
    '''
    A class of PDFWriter objects that take LaTeX source code, compile them into
    PDFs in parallel, and clean up after themselves. It is implemented as a
    context manager.
    '''

    def __init__(self):
        ''' Initializer for PDFwriter objects '''
        self.procs = dict()
        self.cwd = os.getcwd()
        self.sequential = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.clean()

    def call_ext(self, cmd, parent_dir, fname, src):
        '''
        Call external program to compile PDF

        Parameters
        ----------
            cmd : list[str]
                Command to be executed
            parent_dir : string
                Path of the parent directory
            fname : string
                Name of the file to be written sans extension
            src : string
                LaTeX source code
        '''
        src_fname = '{}.tex'.format(fname)
        tmp_dir = tempfile.TemporaryDirectory()
        os.chdir(tmp_dir.name)
        with open(src_fname, 'w') as tmp:
            tmp.write(src)
        # call external program
        proc = subprocess.Popen(cmd + [src_fname], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        self.procs[fname] = {'tmp': tmp_dir, 'parent': parent_dir, 'proc': proc}
        if self.sequential:
            self.clean()

    def write(self, parent_dir, fname, src):
        '''
        Write the source file to disk and launch a pdfLaTeX process

        Parameters
        ----------
            parent_dir : string
                Path of the parent directory
            fname : string
                Name of the file to be written sans extension
            src : string
                LaTeX source code
        '''
        try:
            os.makedirs(parent_dir, exist_ok=True)
        except OSError:
            pass
        # check for the `pdflatex` and `tectonic` executables in PATH, abort if
        # not found
        if shutil.which('tectonic'):
            self.call_ext(['tectonic'], parent_dir, fname, src)
            self.sequential = False
        elif shutil.which('pdflatex'):
            self.sequential = False
            self.call_ext(['pdflatex', '-interaction=nonstopmode'],
                          parent_dir, fname, src)
        else:
            sys.exit('pdflatex and tectonic not found. Do you have LaTeX ' +
                     'installed and configured on your system?')

    def clean(self):
        '''
        Clean up the temporary files created by pdfLaTeX as each process
        finishes
        '''
        for fname, info in self.procs.items():
            tmp = info['tmp']
            parent = info['parent']
            os.chdir(tmp.name)
            info['proc'].wait()
            # if pdfLaTeX showed an error in terminal output, don't clean up
            stdout = info['proc'].stdout.read().decode('utf-8')
            if 'error' in stdout or 'Error' in stdout:
                if 'not found' in stdout:
                    print('Encountered missing package when processing ' +
                          '\'{}.tex\'. '.format(fname) + 'One solution is to ' +
                          'install `tectonic` and try again.')
                else:
                    print('`pdflatex` encountered an error when processing ' +
                          '\'{}.tex\'. See log for details.'.format(fname))
                log = fname + '.log'
                shutil.move(os.path.join(tmp.name, log), os.path.join(parent, log))
                tex = fname + '.tex'
                shutil.move(os.path.join(tmp.name, tex), os.path.join(parent, tex))
                continue
            pdf = fname + '.pdf'
            shutil.move(os.path.join(tmp.name, pdf), os.path.join(parent, pdf))
            tmp.cleanup()
        self.procs = dict()
        os.chdir(self.cwd)


class RostersWriter:
    '''
    A class of roster PDF generator objects. The constructor of objects of this
    class takes in the lab number and a list of checkpoints. After construction,
    the generator objects takes in Roster objects through the `add_roster`
    method. The generators create PDF files via the CodeGen objects when run.
    '''

    def __init__(self, lab, checkpoints, config):
        '''
        Initializer for RostersWriter objects

        Parameters
        ----------
            lab : int
                Lab number
            checkpoints : list[str]
                List of checkpoints
        '''
        self.lab = lab
        self.code = []
        self.code_generators = []
        self.procs = dict()
        self.config = config
        self.checkpoints = checkpoints

    def add_roster(self, roster):
        '''
        Add roster to PDF generator

        Parameters
        ----------
            roster : Roster
        '''
        codegen = CodeGen(roster, self.lab, self.checkpoints)
        self.code_generators.append(codegen)

    def run(self):
        ''' Run the PDF generator '''
        print('Working...', end='\r')
        with PDFWriter() as writer:
            folder = 'Lab {} Blank Rosters'.format(self.lab)
            try:
                parent = os.path.join(self.config.paths['rosters'], folder)
            except KeyError:
                parent = os.path.join(os.getcwd(), folder)
            # generate one PDF file per section
            for i, codegen in enumerate(self.code_generators):
                fname = 'Lab {} Section {} Roster'.format(self.lab, codegen.section)
                writer.write(parent, fname, codegen.run())
            # generate a PDF file containing all the rosters
            codegen = sum(self.code_generators, CodeGen.mempty())
            fname = 'Lab {} Rosters (All)'.format(self.lab)
            writer.write(parent, fname, codegen.run())


class StudentName:
    ''' Abstract representation of student names '''

    def __init__(self, name):
        '''
        Parameters
        ----------
            name : string
                Student name in either first-name first or last-name first form
        '''
        self.string = name
        try:
            [last_name, first_name] = name.split(',')
            self.full_name_fnf = None
            self.last_name = last_name.strip()
            self.first_name = first_name.strip()
        except ValueError:
            self.full_name_fnf = name
            self.last_name = None
            self.first_name = None

    def __str__(self):
        ''' Returns the string from which the object is initialized '''
        return self.string

    def __lt__(self, other):
        return self.canonical() < other.canonical()

    def __hash__(self):
        ''' Hash with the canonical form '''
        return hash(self.canonical())

    def __eq__(self, other):
        try:
            return self.canonical() == other.canonical()
        except AttributeError:
            return False

    def canonicalize(self):
        ''' Turn name into its canonical form '''
        return StudentName(self.canonical())

    def canonical(self):
        ''' We treat first-name first as the canonical form '''
        if self.full_name_fnf is None:
            return ' '.join((self.first_name, self.last_name))
        else:
            return self.full_name_fnf


class Roster:
    '''
    A class of objects that encapsulates data for a single section and is
    responsible for random group generation.
    '''

    def __init__(self, section, students):
        '''
        Initializer for Roster objects

        Parameters
        ----------
            section : int
                Section number
            students : list[str]
                List of student names
        '''
        self.section = section
        self.students = students
        self.groups = dict()

    def set_groups(self):
        ''' Divide a section into random groups '''
        n = len(self.students)
        random.shuffle(self.students)
        # the number of groups is such that each group has no less than 4 people
        # and there will be no more than 6 groups
        ngroups = min(6, n // 5 + (0 if n % 5 == 0 else 1))
        # initialize list of groups
        self.groups = {i + 1: [] for i in range(ngroups)}
        # distribute students into groups like how dealers deal cards to players
        for group, student in zip(itertools.cycle(range(ngroups)), self.students):
            self.groups[group + 1].append(student)


class Rosters:
    '''
    A class of objects for Roster object generation. This is defined as a python
    generator that yields Roster objects.
    '''

    def __init__(self, data):
        '''
        Initializer for Rosters objects

        Parameters
        ----------
            data: PublishedData
        '''
        self.data = data
        self.rosters = map(self.generate_roster, self.data.section_numbers)

    def generate_roster(self, section):
        '''
        Build randomized roster for a section

        Parameters
        ----------
            section : int
                Section number

        Returns
        ----------
            Roster: object that contains all the data for the roster of a section
        '''
        # Student names on the generated rosters are formatted first-name first
        students = [name.canonicalize() for name in self.data.students[section]]
        roster = Roster(section, students)
        roster.set_groups()
        return roster

    def __iter__(self):
        return self.rosters


class MaybeOverwrite(abc.ABC):
    '''
    A metaclass that automatically renames output if there is a name conflict on
    disk
    '''

    @abc.abstractmethod
    def write_to_path(self, fpath):
        '''
        A method to write to the designated path that a child class must
        provide

        Parameters
        ----------
            fpath : string
                Path to destination
        '''
        pass

    def maybe_overwrite(self, fpath, inplace):
        '''
        Write to the given path. Renames output by appending a number to the
        path if destination already exists unless the inplace flag is True.

        Parameters
        ----------
            fpath : string
                Path to destination
            inplace : bool
                Flag to indicate if we should overwrite or rename if there is
                conflict
        '''
        prefix, ext = os.path.splitext(fpath)
        if os.path.exists(fpath) and not inplace:
            for i in itertools.count(1):
                alt_path = '{}({}){}'.format(prefix, i, ext)
                if not os.path.exists(alt_path):
                    fpath = alt_path
                    break
        self.write_to_path(fpath)


class SummaryAttendanceSheet(MaybeOverwrite):
    '''
    A class of SummaryAttendanceSheet generator objects. After object
    construction, it takes in Roster objects through the `add_roster` method and
    it generates an Excel file when the `write` method is called.
    '''

    def __init__(self, lab, checkpoints, config):
        '''
        Initializer for SummaryAttendanceSheet objects

        Parameters
        ----------
            lab : int
                Lab number
            checkpoints : list[str]
                List of checkpoints
            config : Configuration
                Loaded configuration file
        '''
        self.lab = lab
        self.checkpoints = checkpoints
        # we temporarily store each sheet as an individual csv
        self.sheets = dict()
        self.config = config

    def add_sheet(self, roster):
        '''
        Add sheet to Excel file

        Parameters
        ----------
            roster : Roster
        '''
        # create the column headers in plain text
        chkpt = ','.join(self.checkpoints)
        ncol = len(chkpt)
        seps = ';' * (ncol - 1)
        sheet = [';;;;;' + seps,
                 'Under the "Signature" column, leave blank if present, enter "Absent" if absent, describe circumstances if student left soon after quiz;;;;;' + seps,
                 'Under the "Late" column, enter the amount of time if they are late;;;;;' + seps,
                 ';;;;;' + seps,
                 'Signature;Late;Group;Student;TA Check;' + chkpt]
        # create rows of the roster in plain text
        for group_number, students in sorted(roster.groups.items()):
            for student in students:
                sheet.append(';;{};{};;'.format(group_number, student) + seps)
        # read the plain text as a CSV and add as a sheet
        self.sheets[roster.section] = pandas.read_csv(io.StringIO('\n'.join(sheet)), sep=';')

    def write_to_path(self, fpath):
        '''
        Concrete method implementation for MaybeOverwrite. See corresponding
        docs.
        '''
        summary_page = [',']
        summary_page += ['Lab {},Check if complete'.format(self.lab)]
        summary_page += ['Section {}'.format(s) for s in sorted(self.sheets.keys())]
        summary_page = pandas.read_csv(io.StringIO('\n'.join(summary_page)))
        with pandas.ExcelWriter(fpath) as writer:
            summary_page.to_excel(writer, sheet_name='summary', index=False, header=None)
            for section, sheet in sorted(self.sheets.items()):
                sheet.to_excel(writer, sheet_name='section {}'.format(section),
                               index=False, header=None)
            ft = openpyxl.styles.Font(color='FF0000')
            for sheet in writer.sheets:
                worksheet = writer.book[sheet]
                worksheet.font = openpyxl.styles.Font(name='Arial')
                worksheet.column_dimensions['D'].width = 25
                if sheet != 'summary':
                    for cell in ['A1', 'A2', 'A3']:
                        worksheet[cell].font = ft

    def write(self):
        ''' Write to file'''
        # FIXME: should take path as arg instead of taking config to make this
        # a more robust class
        try:
            parent = self.config.paths['rosters']
        except KeyError:
            parent = os.getcwd()
        fpath = os.path.join(parent, 'Lab {} Summary Attendance.xlsx'.format(self.lab))
        self.maybe_overwrite(fpath, inplace=False)


class PublishedData(MaybeOverwrite):
    ''' In-memory representation of data published on Canvas '''

    def __init__(self, fpath, config):
        '''
        Parameters
        ----------
            fpath : string
                Path to the data file to be read
            config : MutableMapping[str, Any]
                The object returned by the TOML parser
        '''
        self.config = config
        rs = r'(^.*(Q|q)uiz.*\(\d+\)$|^.*(C|c)onclusion.*\(\d+\)$|Student|Section|ID|SIS User ID|SIS Login ID)'
        try:
            self.data = pandas.read_csv(fpath, dtype={'ID': str}, skiprows=[1, 2]) \
                              .filter(regex=rs, axis=1) \
                              .set_index('Section', drop=False) \
                              .filter(regex=r'\[\d+\]$', axis=0)
            self.data['Student'] = self.data['Student'].apply(lambda x: StudentName(x))
        except OSError:
            sys.exit('"{}" does not exist. Check your input path.'.format(fpath))

    @property
    @functools.lru_cache
    def students(self):
        '''
        List of students by section

        Returns
        ----------
            dict[int, string]
        '''
        students = self.data['Student']
        sections = self.data.index
        return dict((self.section_number(s), list(students[s].squeeze()))
                    for s in sections)

    @property
    def section_numbers(self):
        ''' List of section numbers '''
        return list(self.section_number(s) for s in set(self.data.index))

    @property
    def quizzes(self):
        return self.data.filter(regex=r'^.*(Q|q)uiz.*\(\d+\)$', axis=1).columns

    def update(self, current_data_fpath):
        '''
        Update Canvas data using the Excel spreadsheet containing the most
        up-to-date grades

        Parameters
        ----------
            current_data_fpath : string
                Path to the latest data spreadsheet
        '''
        sheets = dict()
        for section in set(self.data.index):
            n = self.section_number(section)
            sheets[n] = self.data.loc[section].set_index('Student', drop=False)
        current_data = TAGradeSheets(current_data_fpath, self.config)
        for section, sheet in current_data.data.items():
            sheets[section].update(sheet.set_index('Student', drop=False))
        self.data = pandas.concat(sheets.values()).set_index('Section', drop=False)

    def write_to_path(self, fpath):
        '''
        Concrete method implementation for MaybeOverwrite. See corresponding
        docs.
        '''
        # drop quiz data since that has never been updated
        r = r'(^.*(C|c)onclusion.*\(\d+\)$|Student|Section|ID|SIS User ID|SIS Login ID)'
        data = self.data.reset_index(drop=True).filter(regex=r, axis=1)
        data.to_csv(fpath, index=False)

    def write(self, inplace):
        '''
        Export data to a CSV file to be uploaded to Canvas

        Parameters
        ----------
            inplace : bool
                Flag to indicate if we should overwrite or rename if there is
                conflict
        '''
        self.maybe_overwrite('updated-for-canvas.csv', inplace)

    @staticmethod
    def section_number(string):
        '''
        Extract the section number from a given section string from a Canvas
        data file

        Parameters
        ----------
            string : str
                Section string on Canvas

        Returns
        ----------
            int: section number
        '''
        s = string.split(' ')[-2]
        return int(s)


class TAGradeSheets(MaybeOverwrite):
    '''
    In-memory representation of the Excel spreadsheet for TA use on the shared
    drive
    '''

    def __init__(self, fpath, config):
        '''
        Parameters
        ----------
            fpath : str
                Path to the Excel file containing the latest data
            config : MutableMapping[str, Any]
                The object returned by the TOML parser
        '''
        self.config = config
        if fpath is not None:
            try:
                excel = pandas.read_excel(fpath, sheet_name=None)
            except OSError:
                sys.exit('Invalid path: {}'.format(fpath))
            self.data = dict()
            for section, sheet in excel.items():
                section_number = int(section.split(' ')[1])
                try:
                    # drop rows where the student name is blank (most likely TAs
                    # put an extra row for stats)
                    data = sheet.set_index('Student') \
                                .drop(labels=numpy.nan, axis=0) \
                                .reset_index()
                except KeyError:
                    data = sheet
                data['Student'] = data['Student'].apply(lambda x: StudentName(x))
                self.data[section_number] = data

    @classmethod
    def from_published(cls, published, config):
        '''
        Alternative constructor for the class using exported data from Canavs

        Parameters
        ----------
            published : PublishedData
                Parsed data exported from Canvas
            config : Configuration
                The loaded configuration file

        Returns
        ----------
            obj : TAGradeSheets
        '''
        sheets = dict()
        for section in set(published.data.index):
            grading_sheet = published.data.loc[section].sort_values(by=['Student'])
            n = PublishedData.section_number(section)
            r = r'(^.*(C|c)onclusion.*\(\d+\)$|Student)'
            sheet = grading_sheet.filter(regex=r).copy()
            # For TA grading sheets we put first-name before last-name
            sheet['Student'] = sheet['Student'].apply(lambda x: x.canonicalize())
            sheets[n] = sheet
        obj = cls(None, config)
        obj.data = sheets
        return obj

    def write_to_path(self, fpath):
        '''
        Concrete method implementation for MaybeOverwrite. See corresponding
        docs.
        '''
        with pandas.ExcelWriter(fpath) as writer:
            for section, sheet in sorted(self.data.items()):
                sheet_name = 'section {}'.format(section)
                sheet.to_excel(writer, sheet_name=sheet_name, index=False)
                worksheet = writer.book[sheet_name]
                ncol = worksheet.max_column
                for letter in itertools.islice(string.ascii_uppercase, ncol):
                    worksheet.column_dimensions[letter].width = 25
                worksheet.freeze_panes = worksheet['B1']

    def write(self, inplace):
        '''
        Write to disk as Excel sheet

        Parameters
        ----------
            inplace : bool
                Overwrite existing file if true
        '''
        try:
            fpath = self.config.paths['current-grade-sheet']
            if os.path.splitext(fpath)[1] != '.xlsx':
                sys.exit('Error: \'{}\' is not an Excel file name'.format(fpath))
        except KeyError:
            fpath = os.path.join(os.getcwd(), 'updated-ta-grade-sheet.xlsx')
        self.maybe_overwrite(fpath, inplace)

    @property
    def assignments(self):
        ''' List of assignments '''
        r = r'^.*(C|c)onclusion.*\(\d+\)$'
        sheet = list(self.data.values())[0]
        return sorted(sheet.filter(regex=r).columns)

    @property
    def dataframe_with_ta_assignment(self):
        '''
        A single DataFrame containing all the data in the Excel sheet plus a TA
        column
        '''
        sections = set(self.data.keys())
        for section in sections:
            try:
                ta = self.config.section_ta_map[section]
            except KeyError:
                sys.exit('Error: section {} doesn\''.format(section) +
                         'have a TA. Check configuration.')
            self.data[section]['TA'] = ta
        return pandas.concat(self.data.values()).set_index('TA', drop=False)

    def plot_histogram(self, assignment):
        '''
        Plot histograms showing grade distributions of each TA

        Parameters
        ----------
            assignment : str
                Name of the assignment the way it is on Canvas
        '''
        nx = min(4, len(self.config.ta_list))
        ny = min(3, math.ceil(len(self.config.ta_list) / 4))
        figsize = (2 * nx + 1, 2 * ny + 0.5)
        fig = matplotlib.figure.Figure(figsize=figsize, dpi=192)
        main_ax = fig.add_subplot(111, frameon=False,
                                  xlabel='Points', ylabel='Number of Students')
        main_ax.tick_params(labelcolor='none', top=False, bottom=False,
                            left=False, right=False)
        main_ax.grid(linewidth=0)
        for spine in ['top', 'bottom', 'left', 'right']:
            main_ax.spines[spine].set_color('none')

        for i, ta in enumerate(self.config.ta_list):
            ax = fig.add_subplot(ny, nx, i + 1)
            grades = self.dataframe_with_ta_assignment.loc[ta][assignment].squeeze()

            ax.hist(grades[grades != 0], bins=range(0, 11, 1), rwidth=0.9)
            ax.set_xticks(numpy.arange(0, 11, 2))
            ax.set_title(ta)

        fig.tight_layout()
        fh = tempfile.NamedTemporaryFile(suffix='.pdf')
        fig.savefig(fh, format='pdf')
        return fh

    def grade_review_path(self):
        '''
        Find the directory where the grade review report should be saved
        '''
        try:
            parent = self.config.paths['grade-review']
            os.makedirs(parent, exist_ok=True)
        except KeyError:
            parent = os.getcwd()
        return parent

    def plot_box_whisker(self, assignment):
        '''
        Make a box and whisker plot showing grade distributions for each TA

        Parameters
        ----------
            assignment : str
                Name of the assignment the way it is on Canvas
        '''
        nx = min(6.5, max(3, len(self.config.ta_list) * 0.7))
        fig = matplotlib.figure.Figure(figsize=(nx + 0.2, 3.5), dpi=192)
        ax = fig.add_subplot(111)
        all_data = self.dataframe_with_ta_assignment
        assignment_data = all_data[all_data != 0].filter(items=[assignment])
        self.data_grouped_by_ta = assignment_data.reset_index().groupby('TA')
        self.data_grouped_by_ta.boxplot(ax=ax, subplots=False, rot=60)
        ax.set_xticklabels(self.config.ta_list)
        ax.set_ylabel('Points')
        fig.tight_layout()
        fh = tempfile.NamedTemporaryFile(suffix='.pdf')
        fig.savefig(fh, format='pdf')
        return fh

    def grade_review(self, assignment):
        '''
        Generate a grade review report

        Parameters
        ----------
            assignment : str
                Name of the assignment the way it is on Canvas
        '''
        if matplotlib.__version__ < '3.6.0':
            matplotlib.style.use('seaborn')
        else:
            matplotlib.style.use('seaborn-v0_8')
        self.generate_report(assignment)

    def generate_report(self, assignment):
        '''
        Generate a grade review report PDF using the plots generated earlier

        Parameters
        ----------
            assignment : str
                Name of the assignment the way it is on Canvas
        '''
        with self.plot_histogram(assignment) as hist:
            with self.plot_box_whisker(assignment) as bw:
                ntas = len(self.config.ta_list)
                means = self.data_grouped_by_ta.mean().squeeze().round(2)
                stds = self.data_grouped_by_ta.std().squeeze().round(2)
                src = [
                    '\\documentclass[11pt]{article}',
                    '\\usepackage{booktabs}',
                    '\\usepackage[sfdefault, lf]{carlito}',
                    '\\usepackage{graphicx}',
                    '\\usepackage[margin=0.75in]{geometry}',
                    '\\pagenumbering{gobble}',
                    '\\begin{document}',
                    '\\begin{center}',
                    '  \\hspace{{3em}}\\begin{{tabular}}{{c{}}}'.format('c' * ntas),
                    '    \\toprule',
                    '    & ' + ' & '.join(self.config.ta_list) + '\\\\',
                    '    \\midrule',
                    '    Mean & ' + ' & '.join(map(str, means)) + '\\\\',
                    '    STD & ' + ' & '.join(map(str, stds)) + '\\\\',
                    '    \\bottomrule',
                    '  \\end{tabular}',
                    '',
                    '  \\includegraphics[height=3.5in]{{{}}}'.format(bw.name),
                    '',
                    '  \\includegraphics[width=\\textwidth]{{{}}}'.format(hist.name),
                    '\\end{center}',
                    '\\end{document}']
                parent = self.grade_review_path()
                with PDFWriter() as writer:
                    writer.write(parent, '{} Report'.format(assignment), '\n'.join(src))


class Configuration:
    ''' A class of objects to represent the configuration file in memory '''

    @classmethod
    def load(cls):
        ''' Parse and load configuration file '''
        obj = cls()
        try:
            obj.parsed = toml.load(Configuration.path())
        except OSError:
            obj.parsed = dict()
        except toml.decoder.TomlDecodeError as e:
            print('Warning: malformed configuration\n  {}'.format(e))
            obj.parsed = dict()
        return obj

    @staticmethod
    def path():
        ''' Path to the configuration file '''
        config_dir = None
        if os.name == 'nt':
            for path in map(os.getenv, ['LOCALAPPDATA', 'APPDATA']):
                if path:
                    config_dir = path
        else:
            xdg_config_home = os.getenv('XDG_CONFIG_HOME')
            if xdg_config_home:
                config_dir = xdg_config_home
            config_dir = os.path.join(os.path.expanduser('~'), '.config')
        if config_dir is not None:
            return os.path.join(config_dir, '1l-tools.toml')

    @property
    def paths(self):
        ''' The "paths" configuration key '''
        return self.parsed['paths']

    @property
    def checkpoints(self):
        ''' The "checkpoints" configuration key '''
        return self.parsed['checkpoints']

    @property
    def ta_assignment(self):
        ''' The "ta-assignment" configuration key '''
        return self.parsed['ta-assignment']

    @property
    def ta_section_map(self):
        ''' TA to section mapping '''
        try:
            return self.ta_assignment
        except KeyError:
            sys.exit('Error: TA assignment not found in configuration. ' +
                     'See example configuration in README.md.')

    @property
    def section_ta_map(self):
        ''' Section to TA mapping '''
        return dict((n, ta) for (ta, ns) in self.ta_section_map.items()
                            for n in ns)

    @property
    def ta_list(self):
        ''' List of TA names '''
        return sorted(self.ta_section_map.keys())


class Program:
    ''' Main program '''

    def __init__(self, args):
        '''
        Parameters
        ----------
            args : argparse.Namespace
                A map-like object returned by argparse
        '''
        self.args = args
        self.config = Configuration.load()

    def open_configuration(self):
        ''' Open the configuration file in an external editor '''
        if os.name == 'nt':
            subprocess.run([self.config.path])
        else:
            try:
                if os.environ['EDITOR']:
                    cmd = os.path.expandvars('$EDITOR')
                    subprocess.run([cmd, self.config.path()])
                else:
                    subprocess.run(['open', self.config.path()])
            except KeyError:
                subprocess.run(['open', self.config.path()])

    def reset(self):
        ''' Reset configuration '''
        try:
            os.remove(self.config.path())
        except FileNotFoundError:
            pass

    def run(self):
        ''' Run the requested sub-program '''
        dispatcher = {'rosters': self.rosters,
                      'review': self.review,
                      'merge': self.merge,
                      'new-spreadsheet': self.new_spreadsheet,
                      'edit-config': self.open_configuration,
                      'reset': self.reset}
        dispatcher[self.args.subcmd]()

    def rosters(self):
        ''' The rosters sub-program '''
        # save rosters as xlsx as well for data entry
        if self.args.checkpoints is None:
            try:
                r = re.compile(r'[\w ]*(\d+)')
                ckpt_map = dict((r.search(k).groups()[0], v)
                                for k, v in self.config.checkpoints.items())
                checkpoints = ckpt_map[self.args.LAB]
            except AttributeError:
                print('Warning: `checkpoints` keys in the configuration file ' +
                      'must end with an integer')
                checkpoints = ['1', '2', '3', '4']
            except KeyError:
                print('Warning: no checkpoints provided. Use default.')
                checkpoints = ['1', '2', '3', '4']
        else:
            checkpoints = self.args.checkpoints

        attendance_sheet = SummaryAttendanceSheet(self.args.LAB, checkpoints,
                                                  self.config)
        writer = RostersWriter(self.args.LAB, checkpoints, self.config)
        data = PublishedData(self.args.DATA, self.config)
        for roster in Rosters(data):
            writer.add_roster(roster)
            attendance_sheet.add_sheet(roster)
        writer.run()
        attendance_sheet.write()

    @staticmethod
    def select(msg, choices):
        ''' Displays a selection prompt for a list of items '''
        for i, choice in sorted(choices.items()):
            print('  {}: {}'.format(i + 1, choice))
        while True:
            selection = input(msg)
            try:
                index = int(selection.strip())
                return choices[index - 1]
            except (ValueError, KeyError):
                print('Only enter a number from the list.')

    def review(self):
        ''' The review sub-program '''
        data = TAGradeSheets(self.args.DATA, self.config)
        assignments = dict(enumerate(data.assignments))
        msg = 'Enter the index of the assignment you would like to review: '
        assignment = self.select(msg, assignments)
        data.grade_review(assignment)

    def merge(self):
        ''' The merge sub-program '''
        data = PublishedData(self.args.PUBLISHED, self.config)
        if self.args.current is not None:
            data.update(self.args.current[0])
        else:
            try:
                fpath = self.config.paths['current-grade-sheet']
                data.update(fpath)
            except KeyError:
                sys.exit('Path to current grade spreadsheet not provided ' +
                         'through the \'--current\' option or configuration ' +
                         'file. Abort.')
        data.write(self.args.inplace)
        TAGradeSheets.from_published(data, self.config).write(self.args.inplace)

    def new_spreadsheet(self):
        ''' The new-spreadsheet sub-program '''
        data = PublishedData(self.args.DATA, self.config)
        TAGradeSheets.from_published(data, self.config).write(self.args.inplace)


if __name__ == '__main__':
    # set up argument parser and help text
    parser = argparse.ArgumentParser(prog='1l')
    sub_ps = parser.add_subparsers(required=True, dest='subcmd')
    roster_gen_p = sub_ps.add_parser('rosters',
                                     help='generate rosters')
    roster_gen_p.add_argument('--checkpoints',
                              help='checkpoints for the lab, one after another, separated only by spaces',
                              nargs='+')
    # roster_gen_p.add_argument('--backend', nargs=1,
    #                           help='backend for PDF generation')
    roster_gen_p.add_argument('LAB', help='lab number. Just the number.')
    roster_gen_p.add_argument('DATA', help='path to the Canvas exported CSV file')
    merge_p = sub_ps.add_parser('merge', help='merge Canvas data with latest data')
    merge_p.add_argument('PUBLISHED', help='path to the Canvas exported CSV file')
    merge_p.add_argument('--current', nargs=1,
                         help='path to the grading sheet TAs filled out')
    merge_p.add_argument('--inplace',
                         action='store_true',
                         help='overwrite the TA grading sheet with an updated version if there is conflict')
    new_spreadsheet_p = sub_ps.add_parser('new-spreadsheet',
                                          help='new grading spreadsheet for the shared drive')
    new_spreadsheet_p.add_argument('DATA', help='path to the Canvas exported CSV file')
    new_spreadsheet_p.add_argument('--inplace',
                                   action='store_true',
                                   help='overwrite if there is conflict')
    grade_review_p = sub_ps.add_parser('review', help='generate grade review report')
    grade_review_p.add_argument('DATA', help='path to the current data spreadsheet')
    # grade_review_p.add_argument('--backend', nargs=1,
    #                             help='backend for PDF generation')
    edit_config_p = sub_ps.add_parser('edit-config',
                                      help='edit the configuration file')
    reset_p = sub_ps.add_parser('reset', help='remove configuration file')

    # parse command-line arguments
    args = parser.parse_args()
    prog = Program(args)
    prog.run()
