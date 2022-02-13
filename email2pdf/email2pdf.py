#!/usr/bin/env python3

from datetime import datetime
from email.header import decode_header
from itertools import chain
from subprocess import Popen, PIPE
from sys import platform as _platform
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
import argparse
import chardet
import email
import functools
import html
import io
import locale
import logging
import logging.handlers
import mimetypes
import os
import os.path
import pprint
import re
import shutil
import sys
import tempfile
import textwrap
import traceback

from PyPDF2 import PdfFileReader, PdfFileWriter
from PyPDF2.generic import NameObject, createStringObject
from bs4 import BeautifulSoup
import magic

assert sys.version_info >= (3, 4)

mimetypes.init()

HEADER_MAPPING = {'Author': 'From',
                  'Title': 'Subject',
                  'X-email2pdf-To': 'To'}

FORMATTED_HEADERS_TO_INCLUDE = ['Subject', 'From', 'To', 'Date']

MIME_TYPES_BLACKLIST = frozenset(['text/html', 'text/plain'])

AUTOCALCULATED_FILENAME_EXTENSION_BLACKLIST = frozenset(['.jpe', '.jpeg'])

AUTOGENERATED_ATTACHMENT_PREFIX = 'floating_attachment'

IMAGE_LOAD_BLACKLIST = frozenset(['emltrk.com', 'trk.email', 'shim.gif'])

WKHTMLTOPDF_ERRORS_IGNORE = frozenset([r'QFont::setPixelSize: Pixel size <= 0 \(0\)',
                                       r'Invalid SOS parameters for sequential JPEG',
                                       r'libpng warning: Out of place sRGB chunk',
                                       r'Exit with code 1 due to network error: ContentNotFoundError',
                                       r'Exit with code 1 due to network error: ProtocolUnknownError',
                                       r'Exit with code 1 due to network error: UnknownContentError',
                                       r'libpng warning: iCCP: known incorrect sRGB profile'])

WKHTMLTOPDF_EXTERNAL_COMMAND = 'wkhtmltopdf'


def main(argv, syslog_handler, syserr_handler):
    logger = logging.getLogger('email2pdf')
    warning_count_filter = WarningCountFilter()
    logger.addFilter(warning_count_filter)

    proceed, args = handle_args(argv)

    if not proceed:
        return (False, False)

    if args.enforce_syslog and not syslog_handler:
        raise FatalException("Required syslog socket was not found.")

    if syslog_handler:
        if args.verbose > 0:
            syslog_handler.setLevel(logging.DEBUG)
        else:
            syslog_handler.setLevel(logging.INFO)

    if syserr_handler:
        if args.verbose > 1:
            syserr_handler.setLevel(logging.DEBUG)
        elif args.verbose == 1:
            syserr_handler.setLevel(logging.INFO)
        elif not args.mostly_hide_warnings:
            syserr_handler.setLevel(logging.WARNING)
        else:
            syserr_handler.setLevel(logging.ERROR)

    logger.info("Options used are: " + str(args))

    if not shutil.which(WKHTMLTOPDF_EXTERNAL_COMMAND):
        raise FatalException("email2pdf requires wkhtmltopdf to be installed - please see "
                             "https://github.com/andrewferrier/email2pdf/blob/master/README.md#installing-dependencies "
                             "for more information.")

    output_directory = os.path.normpath(args.output_directory)

    if not os.path.exists(output_directory):
        raise FatalException("output-directory does not exist.")

    output_file_name = get_output_file_name(args, output_directory)
    logger.info("Output file name is: " + output_file_name)

    set_up_warning_logger(logger, output_file_name)

    input_data = get_input_data(args)
    logger.debug("Email input data is: " + input_data)

    input_email = get_input_email(input_data)
    (payload, parts_already_used) = handle_message_body(args, input_email)
    logger.debug("Payload after handle_message_body: " + str(payload))

    if args.body:
        payload = remove_invalid_urls(payload)

        if args.headers:
            header_info = get_formatted_header_info(input_email)
            logger.info("Header info is: " + header_info)
            payload = header_info + payload

        logger.debug("Final payload before output_body_pdf: " + payload)
        output_body_pdf(input_email, bytes(payload, 'UTF-8'), output_file_name)

    if args.attachments:
        number_of_attachments = handle_attachments(input_email,
                                                   output_directory,
                                                   args.add_prefix_date,
                                                   args.ignore_floating_attachments,
                                                   parts_already_used)

    if (not args.body) and number_of_attachments == 0:
        logger.info("First try: didn't print body (on request) or extract any attachments. Retrying with filenamed parts.")
        parts_with_a_filename = filter_filenamed_parts(parts_already_used)
        if len(parts_with_a_filename) > 0:
            number_of_attachments = handle_attachments(input_email,
                                                       output_directory,
                                                       args.add_prefix_date,
                                                       args.ignore_floating_attachments,
                                                       set(parts_already_used - parts_with_a_filename))

        if number_of_attachments == 0:
            logger.warning("Second try: didn't print body (on request) and still didn't find any attachments even when looked for "
                           "referenced ones with a filename. Giving up.")

    if warning_count_filter.warning_pending:
        with open(get_modified_output_file_name(output_file_name, "_original.eml"), 'w') as original_copy_file:
            original_copy_file.write(input_data)

    return (warning_count_filter.warning_pending, args.mostly_hide_warnings)


def handle_args(argv):
    class ArgumentParser(argparse.ArgumentParser):

        def error(self, message):
            raise FatalException(message)

    parser = ArgumentParser(description="Converts emails to PDFs. "
                            "See https://github.com/andrewferrier/email2pdf for more information.", add_help=False)

    parser.add_argument("-i", "--input-file", default="-",
                        help="File containing input email you wish to read in raw form "
                        "delivered from a MTA. If set to '-' (which is the default), it "
                        "reads from stdin.")

    parser.add_argument("--input-encoding",
                        default=locale.getpreferredencoding(), help="Set the "
                        "expected encoding of the input email (whether on stdin "
                        "or specified with the --input-file option). If not set, "
                        "defaults to this system's preferred encoding, which "
                        "is " + locale.getpreferredencoding() + ".")

    parser.add_argument("-o", "--output-file",
                        help="Output file you wish to print the body of the email to as PDF. Should "
                        "include the complete path, otherwise it defaults to the current directory. If "
                        "this option is not specified, email2pdf picks a date & time-based filename and puts "
                        "the file in the directory specified by --output-directory.")

    parser.add_argument("-d", "--output-directory", default=os.getcwd(),
                        help="If --output-file is not specified, the value of this parameter is used as "
                        "the output directory for the body PDF, with a date-and-time based filename attached. "
                        "In either case, this parameter also specifies the directory in which attachments are "
                        "stored. Defaults to the current directory (i.e. " + os.getcwd() + ").")

    body_attachment_options = parser.add_mutually_exclusive_group()

    body_attachment_options.add_argument("--no-body", dest='body', action='store_false', default=True,
                                         help="Don't parse the body of the email and print it to PDF, just detach "
                                         "attachments. The default is to parse both the body and detach attachments.")

    body_attachment_options.add_argument("--no-attachments", dest='attachments', action='store_false', default=True,
                                         help="Don't detach attachments, just print the body of the email to PDF.")

    parser.add_argument("--headers", action='store_true',
                        help="Add basic email headers (" + ", ".join(FORMATTED_HEADERS_TO_INCLUDE) +
                        ") to the first PDF page. The default is not to do this.")

    parser.add_argument("--add-prefix-date", action="store_true",
                        help="Prepend an ISO-8601 prefix date (e.g. YYYY-MM-DD-) to any attachment filename "
                        "that doesn't have one. Will search through the whole filename for an existing "
                        "date in that format - if not found, it prepends one.")

    parser.add_argument("--ignore-floating-attachments", action="store_true",
                        help="Emails sometimes contain attachments that don't have a filename and aren't "
                        "embedded in the main HTML body of the email using a Content-ID either. By "
                        "default, email2pdf will detach these and use their Content-ID as a filename, "
                        "or autogenerate a filename. If this option is specified, it will instead ignore "
                        "them.")

    parser.add_argument("--enforce-syslog", action="store_true",
                        help="By default email2pdf will use syslog if available and just log to stderr "
                        "if not. If this option is specified, email2pdf will exit with an error if the syslog socket "
                        "can not be located.")

    verbose_options = parser.add_mutually_exclusive_group()

    verbose_options.add_argument("--mostly-hide-warnings", action="store_true",
                                 help="By default email2pdf will output warnings about handling emails to stderr and "
                                 "exit with a non-zero return code if any are encountered, *as well as* outputting a "
                                 "summary file entitled <output_PDF_name>_warnings_and_errors.txt and the original "
                                 "email as <output_PDF_name>_original.eml. Specifying this option disables the first "
                                 "two, so only the additional files are produced - this makes it easier to use email2pdf "
                                 "if it is run on a schedule, as warnings won't cause the same email to be repeatedly "
                                 "retried.")

    verbose_options.add_argument('-v', '--verbose', action='count', default=0,
                                 help="Make the output more verbose. This affects both the output logged to "
                                 "syslog, as well as output to the console. Using this twice makes it doubly verbose.")

    parser.add_argument('-h', '--help', action='store_true',
                        help="Show some basic help information about how to use email2pdf.")

    args = parser.parse_args(argv[1:])

    assert args.body or args.attachments

    if args.help:
        parser.print_help()
        return (False, None)
    else:
        return (True, args)


def get_input_data(args):
    logger = logging.getLogger("email2pdf")

    logger.debug("System preferred encoding is: " + locale.getpreferredencoding())
    logger.debug("System encoding is: " + str(locale.getlocale()))
    logger.debug("Input encoding that will be used is " + args.input_encoding)

    if args.input_file.strip() == "-":
        data = ""
        input_stream = io.TextIOWrapper(sys.stdin.buffer, encoding=args.input_encoding)
        for line in input_stream:
            data += line
    else:
        with open(args.input_file, "r", encoding=args.input_encoding) as input_handle:
            data = input_handle.read()

    return data


def get_input_email(input_data):
    input_email = email.message_from_string(input_data)

    defects = input_email.defects
    for part in input_email.walk():
        defects.extend(part.defects)

    if len(defects) > 0:
        raise FatalException("Defects parsing email: " + pprint.pformat(defects))

    return input_email


def get_output_file_name(args, output_directory):
    if args.output_file:
        output_file_name = args.output_file
        if os.path.isfile(output_file_name):
            raise FatalException("Output file " + output_file_name + " already exists.")
    else:
        output_file_name = get_unique_version(os.path.join(output_directory,
                                                           datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".pdf"))

    return output_file_name


def set_up_warning_logger(logger, output_file_name):
    warning_logger_name = get_modified_output_file_name(output_file_name, "_warnings_and_errors.txt")
    warning_logger = logging.FileHandler(warning_logger_name, delay=True)
    warning_logger.setLevel(logging.WARNING)
    warning_logger.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(warning_logger)


def get_modified_output_file_name(output_file_name, append):
    (partial_name, _) = os.path.splitext(output_file_name)
    partial_name = os.path.join(os.path.dirname(partial_name),
                                os.path.basename(partial_name) + append)
    return partial_name


def handle_message_body(args, input_email):
    logger = logging.getLogger("email2pdf")

    cid_parts_used = set()

    part = find_part_by_content_type(input_email, "text/html")
    if part is None:
        part = find_part_by_content_type(input_email, "text/plain")
        if part is None:
            if not args.body:
                logger.debug("No body parts found, but using --no-body; proceeding.")
                return (None, cid_parts_used)
            else:
                raise FatalException("No body parts found; aborting.")
        else:
            payload = handle_plain_message_body(part)
    else:
        (payload, cid_parts_used) = handle_html_message_body(input_email, part)

    return (payload, cid_parts_used)


def handle_plain_message_body(part):
    logger = logging.getLogger("email2pdf")

    if part['Content-Transfer-Encoding'] == '8bit':
        payload = part.get_payload(decode=False)
        assert isinstance(payload, str)
        logger.info("Email is pre-decoded because Content-Transfer-Encoding is 8bit")
    else:
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        charset = part.get_content_charset()
        if not charset:
            charset = 'utf-8'
            logger.info("Determined email is plain text, defaulting to charset utf-8")
        else:
            logger.info("Determined email is plain text with charset " + str(charset))

        if isinstance(payload, bytes):
            try:
                payload = str(payload, charset)
            except UnicodeDecodeError:
                logger.warning("UnicodeDecodeErrors in plain message body, using 'replace'")
                payload = str(payload, charset, errors='replace')

        payload = "\n".join(    # Wrap long lines, individually
            [ textwrap.fill(line, width=80) for line in payload.splitlines() ]
        )
        payload = html.escape(payload)
        payload = "<html><body><pre>\n" + payload + "\n</pre></body></html>"

    return payload


def handle_html_message_body(input_email, part):
    logger = logging.getLogger("email2pdf")

    cid_parts_used = set()

    payload = part.get_payload(decode=True)
    charset = part.get_content_charset()
    if not charset:
        charset = 'utf-8'
    logger.info("Determined email is HTML with charset " + str(charset))

    try:
        payload_unicode = str(payload, charset)
    except UnicodeDecodeError:
        detection = chardet.detect(payload)
        charset = detection["encoding"]
        logger.info("Detected charset can't decode body; trying again with charset " + charset)
        payload_unicode = str(payload, charset)

    def cid_replace(cid_parts_used, matchobj):
        cid = matchobj.group(1)

        logger.debug("Looking for image for cid " + cid)
        image_part = find_part_by_content_id(input_email, cid)

        if image_part is None:
            image_part = find_part_by_content_type_name(input_email, cid)

        if image_part is not None:
            assert image_part['Content-Transfer-Encoding'] == 'base64'
            image_base64 = image_part.get_payload(decode=False)
            image_base64 = re.sub("[\r\n\t]", "", image_base64)
            image_decoded = image_part.get_payload(decode=True)
            mime_type = get_mime_type(image_decoded)
            cid_parts_used.add(image_part)
            return "data:" + mime_type + ";base64," + image_base64
        else:
            logger.warning("Could not find image cid " + cid + " in email content.")
            return "broken"

    payload = re.sub(r'cid:([\w_@.-]+)', functools.partial(cid_replace, cid_parts_used),
                     payload_unicode)

    return (payload, cid_parts_used)


def output_body_pdf(input_email, payload, output_file_name):
    logger = logging.getLogger("email2pdf")

    wkh2p_process = Popen([WKHTMLTOPDF_EXTERNAL_COMMAND, '-q', '--load-error-handling', 'ignore',
                           '--load-media-error-handling', 'ignore', '--encoding', 'utf-8', '-',
                           output_file_name], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    output, error = wkh2p_process.communicate(input=payload)
    assert output == b''

    stripped_error = str(error, 'utf-8')
    if 'XDG_SESSION_TYPE' in os.environ.keys() and os.environ['XDG_SESSION_TYPE'] == 'wayland':
        w_err = r'Warning: Ignoring XDG_SESSION_TYPE=wayland on Gnome. Use QT_QPA_PLATFORM=wayland to run on ' \
                r'Wayland anyway.'
        global WKHTMLTOPDF_ERRORS_IGNORE
        WKHTMLTOPDF_ERRORS_IGNORE = WKHTMLTOPDF_ERRORS_IGNORE.union({w_err})

    for error_pattern in WKHTMLTOPDF_ERRORS_IGNORE:
        (stripped_error, number_of_subs_made) = re.subn(error_pattern, '', stripped_error)
        if number_of_subs_made > 0:
            logger.debug("Made " + str(number_of_subs_made) + " subs with pattern " + error_pattern)

    original_error = str(error, 'utf-8').rstrip()
    stripped_error = stripped_error.rstrip()

    if wkh2p_process.returncode > 0 and original_error == '':
        raise FatalException("wkhtmltopdf failed with exit code " + str(wkh2p_process.returncode) + ", no error output.")
    elif wkh2p_process.returncode > 0 and stripped_error != '':
        raise FatalException("wkhtmltopdf failed with exit code " + str(wkh2p_process.returncode) + ", stripped error: " +
                             stripped_error)
    elif stripped_error != '':
        raise FatalException("wkhtmltopdf exited with rc = 0 but produced unknown stripped error output " + stripped_error)

    add_metadata_obj = {}

    for key in HEADER_MAPPING:
        if HEADER_MAPPING[key] in input_email:
            add_metadata_obj[key] = get_utf8_header(input_email[HEADER_MAPPING[key]])

    add_metadata_obj['Producer'] = 'email2pdf'

    add_update_pdf_metadata(output_file_name, add_metadata_obj)


def remove_invalid_urls(payload):
    logger = logging.getLogger("email2pdf")

    soup = BeautifulSoup(payload, "html5lib")

    for img in soup.find_all('img'):
        if img.has_attr('src'):
            src = img['src']
            lower_src = src.lower()
            if lower_src == 'broken':
                del img['src']
            elif not lower_src.startswith('data'):
                found_blacklist = False

                for image_load_blacklist_item in IMAGE_LOAD_BLACKLIST:
                    if image_load_blacklist_item in lower_src:
                        found_blacklist = True

                if not found_blacklist:
                    logger.debug("Getting img URL " + src)

                    if not can_url_fetch(src):
                        logger.warning("Could not retrieve img URL " + src + ", replacing with blank.")
                        del img['src']
                else:
                    logger.debug("Removing URL that was found in blacklist " + src)
                    del img['src']
            else:
                logger.debug("Ignoring URL " + src)

    return str(soup)


def can_url_fetch(src):
    try:
        encoded_src = src.replace(" ", "%20")
        req = Request(encoded_src)
        urlopen(req)
    except HTTPError:
        return False
    except URLError:
        return False
    except ValueError:
        return False
    else:
        return True


def handle_attachments(input_email, output_directory, add_prefix_date, ignore_floating_attachments, parts_to_ignore):
    logger = logging.getLogger("email2pdf")

    parts = find_all_attachments(input_email, parts_to_ignore)
    logger.debug("Attachments found by handle_attachments: " + str(len(parts)))

    for part in parts:
        filename = extract_part_filename(part)
        if not filename:
            if ignore_floating_attachments:
                continue

            filename = get_content_id(part)
            if not filename:
                filename = AUTOGENERATED_ATTACHMENT_PREFIX

            extension = get_type_extension(part.get_content_type())
            if extension:
                filename = filename + extension

        assert filename is not None

        if add_prefix_date:
            if not re.search(r"\d\d\d\d[-_]\d\d[-_]\d\d", filename):
                filename = datetime.now().strftime("%Y-%m-%d-") + filename

        logger.info("Extracting attachment " + filename)

        full_filename = os.path.join(output_directory, filename)
        full_filename = get_unique_version(full_filename)

        payload = part.get_payload(decode=True)
        with open(full_filename, 'wb') as output_file:
            output_file.write(payload)

    return len(parts)


def add_update_pdf_metadata(filename, update_dictionary):
    # This seems to be the only way to modify the existing PDF metadata.
    #
    # pylint: disable=protected-access, no-member

    def add_prefix(value):
        return '/' + value

    full_update_dictionary = {add_prefix(k): v for k, v in update_dictionary.items()}

    with open(filename, 'rb') as input_file:
        pdf_input = PdfFileReader(input_file)
        pdf_output = PdfFileWriter()

        for page in range(pdf_input.getNumPages()):
            pdf_output.addPage(pdf_input.getPage(page))

        info_dict = pdf_output._info.getObject()

        info = pdf_input.documentInfo

        full_update_dictionary = dict(chain(info.items(), full_update_dictionary.items()))

        for key in full_update_dictionary:
            assert full_update_dictionary[key] is not None
            info_dict.update({NameObject(key): createStringObject(full_update_dictionary[key])})

        os_file_out, temp_file_name = tempfile.mkstemp(prefix="email2pdf_add_update_pdf_metadata", suffix=".pdf")
        # Immediately close the file as created to work around issue on
        # Windows where file cannot be opened twice.
        os.close(os_file_out)

        with open(temp_file_name, 'wb') as file_out:
            pdf_output.write(file_out)

    shutil.move(temp_file_name, filename)


def extract_part_filename(part):
    logger = logging.getLogger("email2pdf")
    filename = part.get_filename()
    if filename is not None:
        logger.debug("Pre-decoded filename: " + filename)
        if decode_header(filename)[0][1] is not None:
            logger.debug("Encoding: " + str(decode_header(filename)[0][1]))
            logger.debug("Filename in bytes: " + str(decode_header(filename)[0][0]))
            filename = str(decode_header(filename)[0][0], (decode_header(filename)[0][1]))
            logger.debug("Post-decoded filename: " + filename)
        return filename
    else:
        return None


def get_unique_version(filename):
    # From here: http://stackoverflow.com/q/183480/27641
    counter = 1
    file_name_parts = os.path.splitext(filename)
    while os.path.isfile(filename):
        filename = file_name_parts[0] + '_' + str(counter) + file_name_parts[1]
        counter += 1
    return filename


def find_part_by_content_type_name(message, content_type_name):
    for part in message.walk():
        if part.get_param('name', header="Content-Type") == content_type_name:
            return part
    return None


def find_part_by_content_type(message, content_type):
    for part in message.walk():
        if part.get_content_type() == content_type:
            return part
    return None


def find_part_by_content_id(message, content_id):
    for part in message.walk():
        if part['Content-ID'] in (content_id, '<' + content_id + '>'):
            return part
    return None


def get_content_id(part):
    content_id = part['Content-ID']
    if content_id:
        content_id = content_id.lstrip('<').rstrip('>')

    return content_id

# part.get_content_disposition() is only available in Python 3.5+, so this is effectively a backport so we can continue to support
# earlier versions of Python 3. It uses an internal API so is a bit unstable and should be replaced with something stable when we
# upgrade to a minimum of Python 3.5. See http://bit.ly/2bHzXtz.


def get_content_disposition(part):
    value = part.get('content-disposition')
    if value is None:
        return None
    c_d = email.message._splitparam(value)[0].lower()
    return c_d


def get_type_extension(content_type):
    filetypes = set(mimetypes.guess_all_extensions(content_type)) - AUTOCALCULATED_FILENAME_EXTENSION_BLACKLIST

    if len(filetypes) > 0:
        return sorted(list(filetypes))[0]
    else:
        return None


def find_all_attachments(message, parts_to_ignore):
    parts = set()

    for part in message.walk():
        if part not in parts_to_ignore and not part.is_multipart():
            if part.get_content_type() not in MIME_TYPES_BLACKLIST:
                parts.add(part)

    return parts


def filter_filenamed_parts(parts):
    new_parts = set()

    for part in parts:
        if part.get_filename() is not None:
            new_parts.add(part)

    return new_parts


def get_formatted_header_info(input_email):
    header_info = ""

    for header in FORMATTED_HEADERS_TO_INCLUDE:
        if input_email[header]:
            decoded_string = get_utf8_header(input_email[header])
            header_info = header_info + '<b>' + header + '</b>: ' + \
                          html.escape(decoded_string) + '<br/>'

    return header_info + '<br/>'

# There are various different magic libraries floating around for Python, and
# this function abstracts that out. The first clause is for `pip3 install
# python-magic`, and the second is for the Ubuntu package python3-magic.


def get_mime_type(buffer_data):
    # pylint: disable=no-member
    if 'from_buffer' in dir(magic):
        mime_type = magic.from_buffer(buffer_data, mime=True)
        if type(mime_type) is not str:
            # Older versions of python-magic seem to output bytes for the
            # mime_type name. As of Python 3.6+, it seems to be outputting
            # strings directly.
            mime_type = str(magic.from_buffer(buffer_data, mime=True), 'utf-8')
    else:
        m_handle = magic.open(magic.MAGIC_MIME_TYPE)
        m_handle.load()
        mime_type = m_handle.buffer(buffer_data)

    return mime_type


def get_utf8_header(header):
    # There is a simpler way of doing this here:
    # http://stackoverflow.com/a/21715870/27641. However, it doesn't seem to
    # work, as it inserts a space between certain elements in the string
    # that's not warranted/correct.

    logger = logging.getLogger("email2pdf")

    decoded_header = decode_header(header)
    logger.debug("Decoded header: " + str(decoded_header))
    hdr = ""
    for element in decoded_header:
        if isinstance(element[0], bytes):
            hdr += str(element[0], element[1] or 'ASCII')
        else:
            hdr += element[0]
    return hdr


class WarningCountFilter(logging.Filter):
    # pylint: disable=too-few-public-methods
    warning_pending = False

    def filter(self, record):
        if record.levelno == logging.WARNING:
            self.warning_pending = True
        return True


class FatalException(Exception):

    def __init__(self, value):
        Exception.__init__(self, value)
        self.value = value

    def __str__(self):
        return repr(self.value)


def call_main(argv, syslog_handler, syserr_handler):
    # pylint: disable=bare-except
    logger = logging.getLogger("email2pdf")

    try:
        (warning_pending, mostly_hide_warnings) = main(argv, syslog_handler, syserr_handler)
    except FatalException as exception:
        logger.error(exception.value)
        sys.exit(2)
    except:
        traceback.print_exc()
        sys.exit(3)

    if warning_pending and not mostly_hide_warnings:
        sys.exit(1)
