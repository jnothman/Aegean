#! /usr/bin/env python
from __future__ import print_function

"""
Module for reading at writing catalogs
"""

__author__ = "Paul Hancock"
__version__ = "1.0"
__date__ = "2016-07-26"

# Standard imports
import sys
import os
import numpy as np
import re
import six
from time import gmtime, strftime

# Other AegeanTools
from .models import OutputSource, classify_catalog

# input/output table formats
import astropy
from astropy.table.table import Table
from astropy.table import Column
from astropy.io import ascii
from astropy.io import fits
from astropy.io.votable import from_table, parse_single_table
from astropy.io.votable import writeto as writetoVO

try:
    import h5py

    hdf5_supported = True
except ImportError:
    hdf5_supported = False

import sqlite3

# join the Aegean logger
import logging

log = logging.getLogger('Aegean')


# writing table formats
def check_table_formats(files):
    """
    Determine whether the supplied table filenames are supported formats.
    The format is determined from the extension.
    :param files: A comma-separated string of filenames (no spaces)
    :return:
    """
    cont = True
    formats = get_table_formats()
    for t in files.split(','):
        name, ext = os.path.splitext(t)
        ext = ext[1:].lower()
        if ext not in formats:
            cont = False
            log.warn("Format not supported for {0} ({1})".format(t, ext))
    if not cont:
        log.error("Invalid table format specified.")
    return cont


def show_formats():
    """
    Print a list of table formats that are supported and the extensions that they are assumed to have
    :return:
    """
    fmts = {
        "ann": "Kvis annotation",
        "reg": "DS9 regions file",
        "fits": "FITS Binary Table",
        "hdf5": "HDF-5 format",
        "csv": "Comma separated values",
        "tab": "tabe separated values",
        "tex": "LaTeX table format",
        "html": "HTML table",
        "vot": "VO-Table",
        "xml": "VO-Table",
        "db": "Sqlite3 database",
        "sqlite": "Sqlite3 database"}
    supported = get_table_formats()
    print("Extension |     Description       | Supported?")
    for k in sorted(fmts.keys()):
        print("{0:10s} {1:24s} {2}".format(k, fmts[k], k in supported))
    return


def get_table_formats():
    """
    Return a list of file extensions that are supported (mapped to an output)
    """
    fmts = ['reg', 'fits']
    fmts.extend(['vo', 'vot', 'xml'])
    fmts.extend(['csv', 'tab', 'tex', 'html'])
    if hdf5_supported:
        fmts.append('hdf5')
    else:
        log.info("HDF5 is not supported by your environment")
    # assume this is always possible -> though it may not be on some systems
    fmts.extend(['db', 'sqlite'])
    return fmts


def update_meta_data(meta=None):
    """
    Update the metadata to include the DATE, PROGRAM, and PROGVER keys
    if they do not already exist.
    :param meta:
    :return:
    """
    if meta is None:
        meta = {}
    if 'DATE' not in meta:
        meta['DATE'] = strftime("%Y-%m-%d %H:%M:%S", gmtime())
    if 'PROGRAM' not in meta:
        meta['PROGRAM'] = "AegeanTools.catalogs"
        meta['PROGVER'] = "{0}-({1})".format(__version__, __date__)
    return meta


def save_catalog(filename, catalog, meta=None):
    """
    input:
        filename - name of file to write, format determined by extension
        catalog - a list of sources (OutputSources, SimpleSources, or IslandSource)
    returns:
        nothing
    """
    ascii_table_formats = {'csv': 'csv', 'tab': 'tab', 'tex': 'latex', 'html': 'html'}
    # .ann and .reg are handled by me
    meta = update_meta_data(meta)
    extension = os.path.splitext(filename)[1][1:].lower()
    if extension in ['ann', 'reg']:
        writeAnn(filename, catalog, extension)
    elif extension in ['db', 'sqlite']:
        writeDB(filename, catalog, meta)
    elif extension in ['hdf5', 'fits', 'vo', 'vot', 'xml']:
        write_catalog(filename, catalog, extension, meta)
    elif extension in ascii_table_formats.keys():
        write_catalog(filename, catalog, fmt=ascii_table_formats[extension], meta=meta)
    else:
        log.warning("extension not recognised {0}".format(extension))
        log.warning("You get tab format")
        write_catalog(filename, catalog, fmt='tab')
    return


def load_catalog(filename):
    """
    load a catalog and extract the source positions
    acceptable formats are:
    csv,tab,tex - from astropy.io.ascii
    vo,vot,xml - votable format
    cat - format created by Aegean
    returns [(ra,dec),...]
    """
    supported = get_table_formats()

    fmt = os.path.splitext(filename)[-1][1:].lower()  # extension sans '.'

    if fmt in ['csv', 'tab', 'tex'] and fmt in supported:
        log.info("Reading file {0}".format(filename))
        t = ascii.read(filename)
        catalog = list(zip(t.columns['ra'], t.columns['dec']))

    elif fmt in ['vo', 'vot', 'xml'] and fmt in supported:
        log.info("Reading file {0}".format(filename))
        t = parse_single_table(filename)
        catalog = list(zip(t.array['ra'].tolist(), t.array['dec'].tolist()))

    else:
        log.info("Assuming ascii format, reading first two columns")
        lines = [a.strip().split() for a in open(filename, 'r').readlines() if not a.startswith('#')]
        try:
            catalog = [(float(a[0]), float(a[1])) for a in lines]
        except:
            log.error("Expecting two columns of floats but failed to parse")
            log.error("Catalog file {0} not loaded".format(filename))
            raise Exception("Could not determine file format")

    return catalog


def load_table(filename):
    """

    :param filename:
    :return:
    """
    supported = get_table_formats()

    fmt = os.path.splitext(filename)[-1][1:].lower()  # extension sans '.'

    if fmt in ['csv', 'tab', 'tex'] and fmt in supported:
        log.info("Reading file {0}".format(filename))
        t = ascii.read(filename)
    elif fmt in ['vo', 'vot', 'xml', 'fits', 'hdf5'] and fmt in supported:
        log.info("Reading file {0}".format(filename))
        t = Table.read(filename)
    else:
        log.error("Table format not recognized or supported")
        log.error("{0} [{1}]".format(filename, fmt))
        raise Exception("Table format not recognized or supported")
    return t


def write_table(table, filename):
    try:
        if os.path.exists(filename):
            os.remove(filename)
        table.write(filename)
        log.info("Wrote {0}".format(filename))
    except Exception as e:
        if "Format could not be identified" not in e.message:
            raise e
        else:
            fmt = os.path.splitext(filename)[-1][1:].lower()  # extension sans '.'
            raise Exception("Cannot auto-determine format for {0}".format(fmt))
    return


def table_to_source_list(table, src_type=OutputSource):
    """
    Wrangle a table into a list of sources given by src_type
    :param table: astropy table instance
    :param src_type: an object type for this source, something that derives from SimpleSource is best
    :return:
    """
    source_list = []
    if table is None:
        return source_list

    for row in table:
        # Initialise our object
        src = src_type()
        # look for the columns required by our source object
        for param in src_type.names:
            if param in table.colnames:
                # copy the value to our object
                val = row[param]
                # hack around float32's broken-ness
                if type(val) == np.float32:
                    val = np.float64(val)
                setattr(src, param, val)
        # save this object to our list of sources
        source_list.append(src)
    return source_list


def write_catalog(filename, catalog, fmt=None, meta=None):
    """
    Write a catalog (list of sourcees) to a file with format determined by extension
    :param filename: output file name
    :param catalog: a list of sources
    :param fmt: the format to use (defualt is to guess from extension)
    :param meta: metadata to be used for formats like fits/votable
    """
    if meta is None:
        meta = {}

    def writer(filename, catalog, fmt=None):
        # construct a dict of the data
        # this method preserves the data types in the VOTable
        tab_dict = {}
        name_list = []
        for name in catalog[0].names:
            col_name = name
            if catalog[0].galactic:
                if name.startswith('ra'):
                    col_name = 'lon'+name[2:]
                elif name.endswith('ra'):
                    col_name = name[:-2] + 'lon'
                elif name.startswith('dec'):
                    col_name = 'lat'+name[3:]
                elif name.endswith('dec'):
                    col_name = name[:-3] + 'lat'

            tab_dict[col_name] = [getattr(c, name, None) for c in catalog]
            name_list.append(col_name)
        t = Table(tab_dict, meta=meta)
        # re-order the columns
        t = t[[n for n in name_list]]

        if fmt is not None:
            if fmt in ["vot", "vo", "xml"]:
                vot = from_table(t)
                # description of this votable
                vot.description = repr(meta)
                writetoVO(vot, filename)
            elif fmt in ['hdf5']:
                t.write(filename, path='data', overwrite=True)
            elif fmt in ['fits']:
                writeFITSTable(filename, t)
            else:
                ascii.write(t, filename, fmt)
        else:
            ascii.write(t, filename)
        return

    # sort the sources into types and then write them out individually
    components, islands, simples = classify_catalog(catalog)

    if len(components) > 0:
        new_name = "{1}{0}{2}".format('_comp', *os.path.splitext(filename))
        writer(new_name, components, fmt)
        log.info("wrote {0}".format(new_name))
    if len(islands) > 0:
        new_name = "{1}{0}{2}".format('_isle', *os.path.splitext(filename))
        writer(new_name, islands, fmt)
        log.info("wrote {0}".format(new_name))
    if len(simples) > 0:
        new_name = "{1}{0}{2}".format('_simp', *os.path.splitext(filename))
        writer(new_name, simples, fmt)
        log.info("wrote {0}".format(new_name))
    return


def writeFITSTable(filename, table):
    """

    :param filename:
    :param table:
    :return:
    """

    def FITSTableType(val):
        """
        Return the FITSTable type corresponding to each named parameter in obj
        """
        if isinstance(val, bool):
            types = "L"
        elif isinstance(val, (int, np.int64, np.int32)):
            types = "J"
        elif isinstance(val, (float, np.float64, np.float32)):
            types = "E"
        elif isinstance(val, six.string_types):
            types = "{0}A".format(len(val))
        else:
            log.warning("Column {0} is of unknown type {1}".format(val, type(val)))
            log.warning("Using 5A")
            types = "5A"
        return types

    cols = []
    for name in table.colnames:
        cols.append(fits.Column(name=name, format=FITSTableType(table[name][0]), array=table[name]))
    cols = fits.ColDefs(cols)
    tbhdu = fits.BinTableHDU.from_columns(cols)
    for k in table.meta:
        tbhdu.header['HISTORY'] = ':'.join((k, table.meta[k]))
    tbhdu.writeto(filename, clobber=True)


def writeIslandContours(filename, catalog, fmt):
    """
    Draw a contour around the pixels of each island
    Input:
    filename = file to write
    catalog = [IslandSource, ...]
    """
    if fmt != 'reg':
        log.warning("Format {0} not yet supported".format(fmt))
        log.warning("not writing anything")
        return

    out = open(filename, 'w')
    print("#Aegean island contours", file=out)
    print("#AegeanTools.catalogs version {0}-({1})".format(__version__, __date__), file=out)
    line_fmt = 'image;line({0},{1},{2},{3})'
    text_fmt = 'fk5; text({0},{1}) # text={{{2}}}'
    mas_fmt = 'image; line({1},{0},{3},{2}) #color = yellow'
    x_fmt = 'image; point({1},{0}) # point=x'
    for c in catalog:
        contour = c.contour
        if len(contour) > 1:
            for p1, p2 in zip(contour[:-1], contour[1:]):
                print(line_fmt.format(p1[1] + 0.5, p1[0] + 0.5, p2[1] + 0.5, p2[0] + 0.5), file=out)
            print(line_fmt.format(contour[-1][1] + 0.5, contour[-1][0] + 0.5, contour[0][1] + 0.5,
                                          contour[0][0] + 0.5), file=out)
        # comment out lines that have invalid ra/dec (WCS problems)
        if np.nan in [c.ra, c.dec]:
            print('#', end=' ', file=out)
        # some islands may not have anchors because they don't have any contours
        if len(c.max_angular_size_anchors) == 4:
            print(text_fmt.format(c.ra, c.dec, c.island), file=out)
            print(mas_fmt.format(*[a + 0.5 for a in c.max_angular_size_anchors]), file=out)
        for p1, p2 in c.pix_mask:
            # DS9 uses 1-based instead of 0-based indexing
            print(x_fmt.format(p1 + 1, p2 + 1), file=out)
    out.close()
    return


def writeIslandBoxes(filename, catalog, fmt):
    """
    Draw a box around each island in the given catalog.
    The box simply outlines the pixels used in the fit.
    Input:
        filename = file to write
        catalog = [IslandSource, ...]
    """
    if fmt not in ['reg', 'ann']:
        log.warning("Format not supported for island boxes{0}".format(fmt))
        return  # fmt not supported

    out = open(filename, 'w')
    print("#Aegean Islands", file=out)
    print("#Aegean version {0}-({1})".format(__version__, __date__), file=out)

    if fmt == 'reg':
        print("IMAGE", file=out)
        box_fmt = 'box({0},{1},{2},{3}) #{4}'
    else:
        print("COORD P", file=out)
        box_fmt = 'box P {0} {1} {2} {3} #{4}'

    for c in catalog:
        # x/y swap for pyfits/numpy translation
        ymin, ymax, xmin, xmax = c.extent
        # +1 for array/image offset
        xcen = (xmin + xmax) / 2.0 + 1
        # + 0.5 in each direction to make lines run 'between' DS9 pixels
        xwidth = xmax - xmin + 1
        ycen = (ymin + ymax) / 2.0 + 1
        ywidth = ymax - ymin + 1
        print(box_fmt.format(xcen, ycen, xwidth, ywidth, c.island), file=out)
    out.close()
    return


def writeAnn(filename, catalog, fmt):
    """
    Write an annotation file that can be read by Kvis (.ann) or DS9 (.reg).
    Uses ra/dec from catalog.
    Draws ellipses if bmaj/bmin/pa are in catalog
    Draws 30" circles otherwise
    Input:
        filename - file to write to
        catalog - a list of OutputSource or SimpleSource
        fmt - [.ann|.reg] format to use
    """
    if fmt not in ['reg', 'ann']:
        log.warning("Format not supported for island boxes{0}".format(fmt))
        return  # fmt not supported

    components, islands, simples = classify_catalog(catalog)
    if len(components) > 0:
        catalog = sorted(components)
        suffix = "comp"
    elif len(simples) > 0:
        catalog = simples
        suffix = "simp"
    else:
        catalog = []

    if len(catalog) > 0:
        ras = [a.ra for a in catalog]
        decs = [a.dec for a in catalog]
        if not hasattr(catalog[0], 'a'):  # a being the variable that I used for bmaj.
            bmajs = [30 / 3600.0 for a in catalog]
            bmins = bmajs
            pas = [0 for a in catalog]
        else:
            bmajs = [a.a / 3600.0 for a in catalog]
            bmins = [a.b / 3600.0 for a in catalog]
            pas = [a.pa for a in catalog]

        names = [a.__repr__() for a in catalog]
        if fmt == 'ann':
            new_file = re.sub('.ann$', '_{0}.ann'.format(suffix), filename)
            out = open(new_file, 'w')
            print("#Aegean version {0}-({1})".format(__version__, __date__), file=out)
            print('PA SKY', file=out)
            print('FONT hershey12', file=out)
            print('COORD W', file=out)
            formatter = "ELLIPSE W {0} {1} {2} {3} {4:+07.3f} #{5}\nTEXT W {0} {1} {5}"
        else:  # reg
            new_file = re.sub('.reg$', '_{0}.reg'.format(suffix), filename)
            out = open(new_file, 'w')
            print("#Aegean version {0}-({1})".format(__version__, __date__), file=out)
            print("fk5", file=out)
            formatter = 'ellipse {0} {1} {2:.9f}d {3:.9f}d {4:+07.3f}d # text="{5}"'
            # DS9 has some strange ideas about position angle
            pas = [a - 90 for a in pas]

        for ra, dec, bmaj, bmin, pa, name in zip(ras, decs, bmajs, bmins, pas, names):
            # comment out lines that have invalid or stupid entries
            if np.nan in [ra, dec, bmaj, bmin, pa] or bmaj >= 180:
                print('#', end=' ', file=out)
            print(formatter.format(ra, dec, bmaj, bmin, pa, name), file=out)
        out.close()
        log.info("wrote {0}".format(new_file))
    if len(islands) > 0:
        if fmt == 'reg':
            new_file = re.sub('.reg$', '_isle.reg', filename)
        elif fmt == 'ann':
            log.warning('kvis islands are currently not working')
            return
        else:
            log.warning('format {0} not supported for island annotations'.format(fmt))
            return
        writeIslandContours(new_file, islands, fmt)
        log.info("wrote {0}".format(new_file))

    return


def nulls(x):
    """
    convert values of -1 into None
    :param x: assumed to be float but w/e
    :return: x or None
    """
    if x == -1:
        return None
    else:
        return x


def writeDB(filename, catalog, meta=None):
    """
    Output an sqlite3 database containing one table for each source type
    inputs:
    filename - output filename
    catalog - a catalog of sources to populated the database with
    """

    def sqlTypes(obj, names):
        """
        Return the sql type corresponding to each named parameter in obj
        """
        types = []
        for n in names:
            val = getattr(obj, n)
            if isinstance(val, bool):
                types.append("BOOL")
            elif isinstance(val, (int, np.int64, np.int32)):
                types.append("INT")
            elif isinstance(val, (float, np.float64, np.float32)):  # float32 is bugged and claims not to be a float
                types.append("FLOAT")
            elif isinstance(val, six.string_types):
                types.append("VARCHAR")
            else:
                log.warning("Column {0} is of unknown type {1}".format(n, type(n)))
                log.warning("Using VARCHAR")
                types.append("VARCHAR")
        return types

    if os.path.exists(filename):
        log.warning("overwriting {0}".format(filename))
        os.remove(filename)
    conn = sqlite3.connect(filename)
    db = conn.cursor()
    # determine the column names by inspecting the catalog class
    for t, tn in zip(classify_catalog(catalog), ["components", "islands", "simples"]):
        if len(t) < 1:
            continue  #don't write empty tables
        col_names = t[0].names
        col_types = sqlTypes(t[0], col_names)
        stmnt = ','.join(["{0} {1}".format(a, b) for a, b in zip(col_names, col_types)])
        db.execute('CREATE TABLE {0} ({1})'.format(tn, stmnt))
        stmnt = 'INSERT INTO {0} ({1}) VALUES ({2})'.format(tn, ','.join(col_names), ','.join(['?' for i in col_names]))
        # expend the iterators that are created by python 3+
        data = list(map(nulls, list(r.as_list() for r in t)))
        db.executemany(stmnt, data)
        log.info("Created table {0}".format(tn))
    # metadata add some meta data
    db.execute("CREATE TABLE meta (key VARCHAR, val VARCHAR)")
    for k in meta:
        db.execute("INSERT INTO meta (key, val) VALUES (?,?)", (k, meta[k]))
    conn.commit()
    log.info(db.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall())
    conn.close()
    log.info("Wrote file {0}".format(filename))
    return

