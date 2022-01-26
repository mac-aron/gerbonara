#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# Modified from parser.py by Paulo Henrique Silva <ph.silva@gmail.com>
# Copyright 2014 Hamilton Kibbe <ham@hamiltonkib.be>
# Copyright 2019 Hiroshi Murayama <opiopan@gmail.com>
# Copyright 2021 Jan Götte <code@jaseg.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" This module provides an RS-274-X class and parser.
"""

import re
import math
import warnings
from pathlib import Path
from itertools import count, chain
from io import StringIO
import dataclasses

from .cam import CamFile, FileSettings
from .utils import sq_distance, rotate_point, MM, Inch, units, InterpMode, UnknownStatementWarning
from .aperture_macros.parse import ApertureMacro, GenericMacros
from . import graphic_primitives as gp
from . import graphic_objects as go
from . import apertures
from .excellon import ExcellonFile


def points_close(a, b):
    if a == b:
        return True
    elif a is None or b is None:
        return False
    elif None in a or None in b:
        return False
    else:
        return math.isclose(a[0], b[0]) and math.isclose(a[1], b[1])

class GerberFile(CamFile):
    """ A class representing a single gerber file

    The GerberFile class represents a single gerber file.
    """

    def __init__(self, objects=None, comments=None, import_settings=None, filename=None, generator_hints=None,
            layer_hints=None, file_attrs=None):
        super().__init__(filename=filename)
        self.objects = objects or []
        self.comments = comments or []
        self.generator_hints = generator_hints or []
        self.layer_hints = layer_hints or []
        self.import_settings = import_settings
        self.apertures = [] # FIXME get rid of this? apertures are already in the objects.
        self.file_attrs = file_attrs or {}

    def to_excellon(self):
        new_objs = []
        new_tools = {}
        for obj in self.objects:
            if not isinstance(obj, Line) or isinstance(obj, Arc) or isinstance(obj, Flash) or \
                not isinstance(obj.aperture, CircleAperture):
                raise ValueError('Cannot convert {type(obj)} to excellon!')

            if not (new_tool := new_tools.get(id(obj.aperture))):
                # TODO plating?
                new_tool = new_tools[id(obj.aperture)] = ExcellonTool(obj.aperture.diameter)
            new_obj = dataclasses.replace(obj, aperture=new_tool)
            
        return ExcellonFile(objects=new_objs, comments=self.comments)

    def merge(self, other):
        """ Merge other GerberFile into this one """
        if other is None:
            return

        self.import_settings = None
        self.comments += other.comments

        # dedup apertures
        new_apertures = {}
        replace_apertures = {}
        mock_settings = FileSettings()
        for ap in self.apertures + other.apertures:
            gbr = ap.to_gerber(mock_settings)
            if gbr not in new_apertures:
                new_apertures[gbr] = ap
            else:
                replace_apertures[id(ap)] = new_apertures[gbr]
        self.apertures = list(new_apertures.values())

        self.objects += other.objects
        for obj in self.objects:
            # If object has an aperture attribute, replace that aperture.
            if (ap := replace_apertures.get(id(getattr(obj, 'aperture', None)))):
                obj.aperture = ap

        # dedup aperture macros
        macros = { m.to_gerber(): m
                for m in [ GenericMacros.circle, GenericMacros.rect, GenericMacros.obround, GenericMacros.polygon] }
        for ap in new_apertures.values():
            if isinstance(ap, apertures.ApertureMacroInstance):
                macro_grb = ap.macro.to_gerber() # use native unit to compare macros
                if macro_grb in macros:
                    ap.macro = macros[macro_grb]
                else:
                    macros[macro_grb] = ap.macro

        # make macro names unique
        seen_macro_names = set()
        for macro in macros.values():
            i = 2
            while (new_name := f'{macro.name}{i}') in seen_macro_names:
                i += 1
            macro.name = new_name
            seen_macro_names.add(new_name)

    def dilate(self, offset, unit=MM, polarity_dark=True):
        self.apertures = [ aperture.dilated(offset, unit) for aperture in self.apertures ]

        offset_circle = CircleAperture(offset, unit=unit)
        self.apertures.append(offset_circle)

        new_primitives = []
        for p in self.primitives:

            p.polarity_dark = polarity_dark

            # Ignore Line, Arc, Flash. Their actual dilation has already been done by dilating the apertures above.
            if isinstance(p, Region):
                ol = p.poly.outline
                for start, end, arc_center in zip(ol, ol[1:] + ol[0], p.poly.arc_centers):
                    if arc_center is not None:
                        new_primitives.append(Arc(*start, *end, *arc_center,
                            polarity_dark=polarity_dark, unit=p.unit, aperture=offset_circle))

                    else:
                        new_primitives.append(Line(*start, *end,
                            polarity_dark=polarity_dark, unit=p.unit, aperture=offset_circle))

        # it's safe to append these at the end since we compute a logical OR of opaque areas anyway.
        self.primitives.extend(new_primitives)

    @classmethod
    def open(kls, filename, enable_includes=False, enable_include_dir=None):
        filename = Path(filename)
        with open(filename, "r") as f:
            if enable_includes and enable_include_dir is None:
                enable_include_dir = filename.parent
            return kls.from_string(f.read(), enable_include_dir, filename=filename.name)

    @classmethod
    def from_string(kls, data, enable_include_dir=None, filename=None):
        # filename arg is for error messages
        obj = kls()
        GerberParser(obj, include_dir=enable_include_dir).parse(data, filename=filename)
        return obj

    def generate_statements(self, settings, drop_comments=True):
        yield 'G04 Gerber file generated by Gerbonara*'
        for name, value in self.file_attrs.items():
            attrdef = ','.join([name, *map(str, value)])
            yield f'%TF{attrdef}*%'
        yield '%MOMM*%' if (settings.unit == 'mm') else '%MOIN*%'

        zeros = 'T' if settings.zeros == 'trailing' else 'L' # default to leading if "None" is specified
        notation = 'I' if settings.notation == 'incremental' else 'A' # default to absolute
        number_format = str(settings.number_format[0]) + str(settings.number_format[1])
        yield f'%FS{zeros}{notation}X{number_format}Y{number_format}*%'
        yield '%IPPOS*%'
        yield 'G75'
        yield '%LPD*%'

        if not drop_comments:
            yield 'G04 Comments from original gerber file:*'
            for cmt in self.comments:
                yield f'G04{cmt}*'

        # Always emit gerbonara's generic, rotation-capable aperture macro replacements for the standard C/R/O/P shapes.
        # Unconditionally emitting these here is easier than first trying to figure out if we need them later,
        # and they are only a few bytes anyway.
        am_stmt = lambda macro: f'%AM{macro.name}*\n{macro.to_gerber(unit=settings.unit)}*\n%'
        for macro in [ GenericMacros.circle, GenericMacros.rect, GenericMacros.obround, GenericMacros.polygon ]:
            yield am_stmt(macro)

        processed_macros = set()
        aperture_map = {}
        for number, aperture in enumerate(self.apertures, start=10):

            if isinstance(aperture, apertures.ApertureMacroInstance):
                macro_def = am_stmt(aperture._rotated().macro)
                if macro_def not in processed_macros:
                    processed_macros.add(macro_def)
                    yield macro_def

            yield f'%ADD{number}{aperture.to_gerber(settings)}*%'

            aperture_map[id(aperture)] = number

        def warn(msg, kls=SyntaxWarning):
            warnings.warn(msg, kls)

        gs = GraphicsState(warn=warn, aperture_map=aperture_map, file_settings=settings)
        for primitive in self.objects:
            yield from primitive.to_statements(gs)

        yield 'M02*'

    def __str__(self):
        return f'<GerberFile with {len(self.apertures)} apertures, {len(self.objects)} objects>'

    def save(self, filename, settings=None, drop_comments=True):
        with open(filename, 'w', encoding='utf-8') as f: # Encoding is specified as UTF-8 by spec.
            f.write(self.to_gerber(settings, drop_comments=drop_comments))

    def to_gerber(self, settings=None, drop_comments=True):
        # Use given settings, or use same settings as original file if not given, or use defaults if not imported from a
        # file
        if settings is None:
            settings = self.import_settings.copy() or FileSettings()
            settings.zeros = None
            settings.number_format = (5,6)
        return '\n'.join(self.generate_statements(settings, drop_comments=drop_comments))

    @property
    def is_empty(self):
        return not self.objects

    def __len__(self):
        return len(self.objects)

    def __bool__(self):
        return not self.is_empty

    def offset(self, dx=0,  dy=0, unit=MM):
        # TODO round offset to file resolution
    
        self.objects = [ obj.with_offset(dx, dy, unit) for obj in self.objects ]

    def rotate(self, angle:'radian', center=(0,0), unit=MM):
        """ Rotate file contents around given point.

            Arguments:
            angle -- Rotation angle in radian clockwise.
            center -- Center of rotation (default: document origin (0, 0))

            Note that when rotating by odd angles other than 0, 90, 180 or 270 degree this method may replace standard
            rect and oblong apertures by macro apertures. Existing macro apertures are re-written.
        """
        if math.isclose(angle % (2*math.pi), 0):
            return

        # First, rotate apertures. We do this separately from rotating the individual objects below to rotate each
        # aperture exactly once.
        for ap in self.apertures:
            ap.rotation += angle

        for obj in self.objects:
            obj.rotate(angle, *center, unit)

    def invert_polarity(self):
        for obj in self.objects:
            obj.polarity_dark = not p.polarity_dark
    

class GraphicsState:
    def __init__(self, warn, file_settings=None, aperture_map=None):
        self.image_polarity = 'positive' # IP image polarity; deprecated
        self.polarity_dark = True
        self.point = None
        self.aperture = None
        self.file_settings = None
        self.interpolation_mode = InterpMode.LINEAR
        self.multi_quadrant_mode = None # used only for syntax checking
        self.aperture_mirroring = (False, False) # LM mirroring (x, y)
        self.aperture_rotation = 0 # LR rotation in degree, ccw
        self.aperture_scale = 1 # LS scale factor, NOTE: same for both axes
        # The following are deprecated file-wide settings. We normalize these during parsing.
        self.image_offset = (0, 0)
        self.image_rotation = 0 # IR image rotation in degree ccw, one of 0, 90, 180 or 270; deprecated
        self.image_mirror = (False, False) # IM image mirroring, (x, y); deprecated
        self.image_scale = (1.0, 1.0) # SF image scaling (x, y); deprecated
        self.image_axes = 'AXBY' # AS axis mapping; deprecated
        self._mat = None
        self.file_settings = file_settings
        self.aperture_map = aperture_map or {}
        self.warn = warn
        self.unit_warning = False

    def __setattr__(self, name, value):
        # input validation
        if name == 'image_axes' and value not in [None, 'AXBY', 'AYBX']:
            raise ValueError('image_axes must be either "AXBY", "AYBX" or None')
        elif name == 'image_rotation' and value not in [0, 90, 180, 270]:
            raise ValueError('image_rotation must be 0, 90, 180 or 270')
        elif name == 'image_polarity' and value not in ['positive', 'negative']:
            raise ValueError('image_polarity must be either "positive" or "negative"')
        elif name == 'image_mirror' and len(value) != 2:
            raise ValueError('mirror_image must be 2-tuple of bools: (mirror_a, mirror_b)')
        elif name == 'image_offset' and len(value) != 2:
            raise ValueError('image_offset must be 2-tuple of floats: (offset_a, offset_b)')
        elif name == 'image_scale' and len(value) != 2:
            raise ValueError('image_scale must be 2-tuple of floats: (scale_a, scale_b)')

        # polarity handling
        if name == 'image_polarity': # global IP statement image polarity, can only be set at beginning of file
            if getattr(self, 'image_polarity', None) == 'negative':
                self.polarity_dark = False # evaluated before image_polarity is set below through super().__setattr__

        elif name == 'polarity_dark': # local LP statement polarity for subsequent objects
            if self.image_polarity == 'negative':
                value = not value

        super().__setattr__(name, value)

    def _update_xform(self):
        a, b = 1, 0
        c, d = 0, 1
        off_x, off_y = self.image_offset

        if self.image_mirror[0]:
            a = -1
        if self.image_mirror[1]:
            d = -1

        a *= self.image_scale[0]
        d *= self.image_scale[1]

        if self.image_rotation == 90:
            a, b, c, d = 0, -d, a, 0
            off_x, off_y = off_y, -off_x
        elif self.image_rotation == 180:
            a, b, c, d = -a, 0, 0, -d
            off_x, off_y = -off_x, -off_y
        elif self.image_rotation == 270:
            a, b, c, d = 0, d, -a, 0
            off_x, off_y = -off_y, off_x

        self.image_offset = off_x, off_y
        self._mat = a, b, c, d
    
    def map_coord(self, x, y, relative=False):
        if self._mat is None:
            self._update_xform()
        a, b, c, d = self._mat

        if not relative:
            rx, ry = (a*x + b*y + self.image_offset[0]), (c*x + d*y + self.image_offset[1])
            return rx, ry
        else:
            # Apply mirroring, scale and rotation, but do not apply offset
            rx, ry = (a*x + b*y), (c*x + d*y)
            return rx, ry

    def flash(self, x, y, attrs=None):
        if self.file_settings.unit is None and not self.unit_warning:
            self.warn('Gerber file does not contain a unit definition.')
            self.unit_warning = True
        attrs = attrs or {}
        self.update_point(x, y)
        return go.Flash(*self.map_coord(*self.point), self.aperture,
                polarity_dark=self.polarity_dark,
                unit=self.file_settings.unit,
                attrs=attrs)

    def interpolate(self, x, y, i=None, j=None, aperture=True, multi_quadrant=False, attrs=None):
        if self.point is None:
            self.warn('D01 interpolation without preceding D02 move.')
            self.point = (0, 0)
        old_point = self.map_coord(*self.update_point(x, y))

        if self.file_settings.unit is None and not self.unit_warning:
            self.warn('Gerber file does not contain a unit definition.')
            self.unit_warning = True

        if aperture:
            if not self.aperture:
                raise SyntaxError('Interpolation attempted without selecting aperture first')

            if math.isclose(self.aperture.equivalent_width(), 0):
                self.warn('D01 interpolation with a zero-size aperture. This is invalid according to spec, '
                        'however, we pass through the created objects here. Note that these will not show up in e.g. '
                        'SVG output since their line width is zero.')

        if self.interpolation_mode == InterpMode.LINEAR:
            if i is not None or j is not None:
                raise SyntaxError("i/j coordinates given for linear D01 operation (which doesn't take i/j)")

            return self._create_line(old_point, self.map_coord(*self.point), aperture, attrs)

        else:

            if i is None and j is None:
                self.warn('Linear segment implied during arc interpolation mode through D01 w/o I, J values')
                return self._create_line(old_point, self.map_coord(*self.point), aperture, attrs)

            else:
                if i is None:
                    self.warn('Arc is missing I value')
                    i = 0
                if j is None:
                    self.warn('Arc is missing J value')
                    j = 0
                return self._create_arc(old_point, self.map_coord(*self.point), (i, j), aperture, multi_quadrant, attrs)

    def _create_line(self, old_point, new_point, aperture=True, attrs=None):
        attrs = attrs or {}
        return go.Line(*old_point, *new_point, self.aperture if aperture else None,
                polarity_dark=self.polarity_dark, unit=self.file_settings.unit, attrs=attrs)

    def _create_arc(self, old_point, new_point, control_point, aperture=True, multi_quadrant=False, attrs=None):
        attrs = attrs or {}
        clockwise = self.interpolation_mode == InterpMode.CIRCULAR_CW

        if not multi_quadrant:
            return go.Arc(*old_point, *new_point, *self.map_coord(*control_point, relative=True),
                    clockwise=clockwise, aperture=(self.aperture if aperture else None),
                    polarity_dark=self.polarity_dark, unit=self.file_settings.unit, attrs=attrs)

        else:
            if math.isclose(old_point[0], new_point[0]) and math.isclose(old_point[1], new_point[1]):
                # In multi-quadrant mode, an arc with identical start and end points is not rendered at all. Only in
                # single-quadrant mode it is rendered as a full circle.
                return None

            # Super-legacy. No one uses this EXCEPT everything that mentor graphics / siemens make uses this m(
            (cx, cy) = self.map_coord(*control_point, relative=True)

            arc = lambda cx, cy: go.Arc(*old_point, *new_point, cx, cy,
                    clockwise=clockwise, aperture=(self.aperture if aperture else None),
                    polarity_dark=self.polarity_dark, unit=self.file_settings.unit, attrs=attrs)
            arcs = [ arc(cx, cy), arc(-cx, cy), arc(cx, -cy), arc(-cx, -cy) ]
            arcs = sorted(arcs, key=lambda a: a.numeric_error())

            for a in arcs:
                d = gp.point_line_distance(old_point, new_point, (old_point[0]+a.cx, old_point[1]+a.cy))
                if (d > 0) == clockwise:
                    return a
            assert False

    def update_point(self, x, y, unit=None):
        old_point = self.point
        x, y = MM(x, unit), MM(y, unit)

        if (x is None or y is None) and self.point is None:
            self.warn('Coordinate omitted from first coordinate statement in the file. This is likely a Siemens '
                    'file. We pretend the omitted coordinate was 0.')
            self.point = (0, 0)

        if x is None:
            x = self.point[0]
        if y is None:
            y = self.point[1]

        self.point = (x, y)
        return old_point

    # Helpers for gerber generation
    def set_polarity(self, polarity_dark):
        if self.polarity_dark != polarity_dark:
            self.polarity_dark = polarity_dark
            yield '%LPD*%' if polarity_dark else '%LPC*%'

    def set_aperture(self, aperture):
        if self.aperture != aperture:
            self.aperture = aperture
            yield f'D{self.aperture_map[id(aperture)]}*'

    def set_current_point(self, point, unit=None):
        point_mm = MM(point[0], unit), MM(point[1], unit)
        # TODO calculate appropriate precision for math.isclose given file_settings.notation

        if not points_close(self.point, point_mm):
            self.point = point_mm
            x = self.file_settings.write_gerber_value(point[0], unit=unit)
            y = self.file_settings.write_gerber_value(point[1], unit=unit)
            yield f'X{x}Y{y}D02*'

    def set_interpolation_mode(self, mode):
        if self.interpolation_mode != mode:
            self.interpolation_mode = mode
            yield self.interpolation_mode_statement()

    def interpolation_mode_statement(self):
        return {
                InterpMode.LINEAR: 'G01',
                InterpMode.CIRCULAR_CW: 'G02',
                InterpMode.CIRCULAR_CCW: 'G03'}[self.interpolation_mode]


class GerberParser:
    NUMBER = r"[\+-]?\d+"
    DECIMAL = r"[\+-]?\d+([.]?\d+)?"
    NAME = r"[a-zA-Z_$\.][a-zA-Z_$\.0-9+\-]+"

    STATEMENT_REGEXES = {
        'region_start': r'G36$',
        'region_end': r'G37$',
        'coord': fr"(?P<interpolation>G0?[123]|G74|G75)?(X(?P<x>{NUMBER}))?(Y(?P<y>{NUMBER}))?" \
            fr"(I(?P<i>{NUMBER}))?(J(?P<j>{NUMBER}))?" \
            fr"(?P<operation>D0?[123])?$",
        'aperture': r"(G54|G55)?D(?P<number>\d+)",
        # Allegro combines format spec and unit into one long illegal extended command.
        'allegro_format_spec': r"FS(?P<zero>(L|T|D))?(?P<notation>(A|I))[NG0-9]*X(?P<x>[0-7][0-7])Y(?P<y>[0-7][0-7])[DM0-9]*\*MO(?P<unit>IN|MM)",
        'unit_mode': r"MO(?P<unit>(MM|IN))",
        'format_spec': r"FS(?P<zero>(L|T|D))?(?P<notation>(A|I))[NG0-9]*X(?P<x>[0-7][0-7])Y(?P<y>[0-7][0-7])[DM0-9]*",
        'allegro_legacy_params': fr'^IR(?P<rotation>[0-9]+)\*IP(?P<polarity>(POS|NEG))\*OF(A(?P<a>{DECIMAL}))?(B(?P<b>{DECIMAL}))?\*MI(A(?P<ma>0|1))?(B(?P<mb>0|1))?\*SF(A(?P<sa>{DECIMAL}))?(B(?P<sb>{DECIMAL}))?',
        'load_polarity': r"LP(?P<polarity>(D|C))",
        # FIXME LM, LR, LS
        'load_name': r"LN(?P<name>.*)",
        'offset': fr"OF(A(?P<a>{DECIMAL}))?(B(?P<b>{DECIMAL}))?",
        'include_file': r"IF(?P<filename>.*)",
        'image_name': r"^IN(?P<name>.*)",
        'axis_selection': r"^AS(?P<axes>AXBY|AYBX)",
        'image_polarity': r"^IP(?P<polarity>(POS|NEG))",
        'image_rotation': fr"^IR(?P<rotation>{NUMBER})",
        'mirror_image': r"^MI(A(?P<ma>0|1))?(B(?P<mb>0|1))?",
        'scale_factor': fr"^SF(A(?P<sa>{DECIMAL}))?(B(?P<sb>{DECIMAL}))?",
        'aperture_definition': fr"ADD(?P<number>\d+)(?P<shape>C|R|O|P|{NAME})(,(?P<modifiers>[^,%]*))?$",
        'aperture_macro': fr"AM(?P<name>{NAME})\*(?P<macro>[^%]*)",
        'siemens_garbage': r'^ICAS$',
        'old_unit':r'(?P<mode>G7[01])',
        'old_notation': r'(?P<mode>G9[01])',
        'eof': r"M0?[02]",
        'ignored': r"(?P<stmt>M01)",
        # NOTE: The official spec says names can be empty or contain commas. I think that doesn't make sense.
        'attribute': r"(?P<eagle_garbage>G04 #@! %)?(?P<type>TF|TA|TO|TD)(?P<name>[._$a-zA-Z][._$a-zA-Z0-9]*)(,(?P<value>.*))",
        # Eagle file attributes handled above.
        'comment': r"G0?4(?P<comment>[^*]*)",
        }

    STATEMENT_REGEXES = { key: re.compile(value) for key, value in STATEMENT_REGEXES.items() }


    def __init__(self, target, include_dir=None):
        """ Pass an include dir to enable IF include statements (potentially DANGEROUS!). """
        self.target = target
        self.include_dir = include_dir
        self.include_stack = []
        self.file_settings = FileSettings()
        self.graphics_state = GraphicsState(warn=self.warn, file_settings=self.file_settings)
        self.aperture_map = {}
        self.aperture_macros = {}
        self.current_region = None
        self.eof_found = False
        self.multi_quadrant_mode = None # used only for syntax checking
        self.macros = {}
        self.last_operation = None
        self.generator_hints = []
        self.layer_hints = []
        self.file_attrs = {}
        self.object_attrs = {}
        self.aperture_attrs = {}
        self.filename = None
        self.lineno = None
        self.line = None

    def warn(self, msg, kls=SyntaxWarning):
        line_joined = self.line.replace('\n', '\\n')
        warnings.warn(f'{self.filename}:{self.lineno} "{line_joined}": {msg}', kls)

    @classmethod
    def _split_commands(kls, data):
        start = 0
        extended_command = False
        lineno = 1

        for pos, c in enumerate(data):
            if c == '\n':
                lineno += 1

            if c == '%':
                if extended_command:
                    yield lineno, data[start:pos]
                    extended_command = False

                else:
                    # Ignore % inside G04 comments. Eagle uses a completely borked file attribute syntax with unbalanced
                    # percent signs inside G04 comments.
                    if not data[start:pos].startswith('G04'):
                        extended_command = True

                start = pos + 1
                continue

            elif extended_command:
                continue

            if c in '*\r\n':
                word_command = data[start:pos].strip()
                if word_command and word_command != '*':
                    yield lineno, word_command
                start = pos + 1

    def parse(self, data, filename=None):
        # filename arg is for error messages
        filename = self.filename = filename or '<unknown>'

        for lineno, line in self._split_commands(data):
            if not line.strip():
                continue
            line = line.rstrip('*').strip()
            self.lineno, self.line = lineno, line
            # We cannot assume input gerber to use well-formed statement delimiters. Thus, we may need to parse
            # multiple statements from one line.
            if line.strip() and self.eof_found:
                self.warn('Data found in gerber file after EOF.')
            #print(f'Line {lineno}: {line}')

            for name, le_regex in self.STATEMENT_REGEXES.items():
                if (match := le_regex.match(line)):
                    #print(f'    match: {name} / {match}')
                    try:
                        getattr(self, f'_parse_{name}')(match)
                    except Exception as e:
                        #print(f'Line {lineno}: {line}')
                        #print(f'    match: {name} / {match}')
                        raise SyntaxError(f'{filename}:{lineno} "{line}": {e}') from e
                    line = line[match.end(0):]
                    break

            else:
                self.warn(f'Unknown statement found: "{line}", ignoring.', UnknownStatementWarning)
                self.target.comments.append(f'Unknown statement found: "{line}", ignoring.')
        
        self.target.apertures = list(self.aperture_map.values())
        self.target.import_settings = self.file_settings
        self.target.unit = self.file_settings.unit
        self.target.file_attrs = self.file_attrs

        if not self.eof_found:
                    self.warn('File is missing mandatory M02 EOF marker. File may be truncated.')

    def _parse_coord(self, match):
        if match['interpolation'] == 'G01':
            self.graphics_state.interpolation_mode = InterpMode.LINEAR
        elif match['interpolation'] == 'G02':
            self.graphics_state.interpolation_mode = InterpMode.CIRCULAR_CW
        elif match['interpolation'] == 'G03':
            self.graphics_state.interpolation_mode = InterpMode.CIRCULAR_CCW
        elif match['interpolation'] == 'G74':
            self.multi_quadrant_mode = True # used only for syntax checking
        elif match['interpolation'] == 'G75':
            self.multi_quadrant_mode = False

        has_coord = (match['x'] or match['y'] or match['i'] or match['j'])
        if match['interpolation'] in ('G74', 'G75') and has_coord:
            raise SyntaxError('G74/G75 combined with coord')

        x = self.file_settings.parse_gerber_value(match['x'])
        y = self.file_settings.parse_gerber_value(match['y'])
        i = self.file_settings.parse_gerber_value(match['i'])
        j = self.file_settings.parse_gerber_value(match['j'])

        if not (op := match['operation']) and has_coord:
            if self.last_operation == 'D01':
                self.warn('Coordinate statement without explicit operation code. This is forbidden by spec.')
                op = 'D01'

            else:
                if 'siemens' in self.generator_hints:
                    self.warn('Ambiguous coordinate statement. Coordinate statement does not have an operation '\
                                  'mode and the last operation statement was not D01. This is garbage, and forbidden '\
                                  'by spec. but since this looks like a Siemens/Mentor Graphics file, we will let it '\
                                  'slide and treat this as the same as the last operation.')
                    # Yes, we repeat the last op, and don't do a D01. This is confirmed by
                    # resources/siemens/80101_0125_F200_L12_Bottom.gdo which contains an implicit-double-D02
                    op = self.last_operation
                else:
                    raise SyntaxError('Ambiguous coordinate statement. Coordinate statement does not have an '\
                            'operation mode and the last operation statement was not D01. This is garbage, and '\
                            'forbidden by spec.')

        self.last_operation = op

        if op in ('D1', 'D01'):
            if self.graphics_state.interpolation_mode != InterpMode.LINEAR:
                if self.multi_quadrant_mode is None:
                    self.warn('Circular arc interpolation without explicit G75 Single-Quadrant mode statement. '\
                            'This can cause problems with older gerber interpreters.')

                elif self.multi_quadrant_mode:
                    self.warn('Deprecated G74 multi-quadant mode arc found. G74 is bad and you should feel bad.')

            if self.current_region is None:
                # in multi-quadrant mode this may return None if start and end point of the arc are the same.
                obj = self.graphics_state.interpolate(x, y, i, j, multi_quadrant=bool(self.multi_quadrant_mode))
                if obj is not None:
                    self.target.objects.append(obj)
            else:
                obj = self.graphics_state.interpolate(x, y, i, j, aperture=False, multi_quadrant=bool(self.multi_quadrant_mode))
                if obj is not None:
                    self.current_region.append(obj)

        elif op in ('D2', 'D02'):
            self.graphics_state.update_point(x, y)
            if self.current_region:
                # Start a new region for every outline. As gerber has no concept of fill rules or winding numbers,
                # it does not make a graphical difference, and it makes the implementation slightly easier.
                self.target.objects.append(self.current_region)
                self.current_region = go.Region(
                        polarity_dark=self.graphics_state.polarity_dark,
                        unit=self.file_settings.unit)

        elif op in ('D3', 'D03'):
            if self.current_region is None:
                self.target.objects.append(self.graphics_state.flash(x, y))
            else:
                raise SyntaxError('DO3 flash statement inside region')

        else:
            # Do nothing if there is no explicit D code.
            pass

    def _parse_aperture(self, match):
        number = int(match['number'])
        if number < 10:
            raise SyntaxError(f'Invalid aperture number {number}: Aperture number must be >= 10.')

        if number not in self.aperture_map:
            raise SyntaxError(f'Tried to access undefined aperture {number}')

        self.graphics_state.aperture = self.aperture_map[number]

    def _parse_aperture_definition(self, match):
        # number, shape, modifiers
        modifiers = [ float(val) for val in match['modifiers'].strip(' ,').split('X') ] if match['modifiers'] else []

        aperture_classes = {
                'C': apertures.CircleAperture,
                'R': apertures.RectangleAperture,
                'O': apertures.ObroundAperture,
                'P': apertures.PolygonAperture,
            }

        if (kls := aperture_classes.get(match['shape'])):
            if match['shape'] == 'P' and math.isclose(modifiers[0], 0):
                self.warn('Definition of zero-size polygon aperture. This is invalid according to spec.' )

            if match['shape'] in 'RO' and (math.isclose(modifiers[0], 0) or math.isclose(modifiers[1], 0)):
                self.warn('Definition of zero-width and/or zero-height rectangle or obround aperture. This is invalid according to spec.' )

            new_aperture = kls(*modifiers, unit=self.file_settings.unit, attrs=self.aperture_attrs.copy())

        elif (macro := self.aperture_macros.get(match['shape'])):
            new_aperture = apertures.ApertureMacroInstance(macro, modifiers, unit=self.file_settings.unit, attrs=self.aperture_attrs.copy())

        else:
            raise ValueError(f'Aperture shape "{match["shape"]}" is unknown')

        self.aperture_map[int(match['number'])] = new_aperture

    def _parse_aperture_macro(self, match):
        self.aperture_macros[match['name']] = ApertureMacro.parse_macro(
                match['name'], match['macro'], self.file_settings.unit)
    
    def _parse_format_spec(self, match):
        # This is a common problem in Eagle files, so just suppress it
        self.file_settings.zeros = {'L': 'leading', 'T': 'trailing'}.get(match['zero'], 'leading')
        self.file_settings.notation = 'incremental' if match['notation'] == 'I' else 'absolute'

        if match['x'] != match['y']:
            raise SyntaxError(f'FS specifies different coordinate formats for X and Y ({match["x"]} != {match["y"]})')
        self.file_settings.number_format = int(match['x'][0]), int(match['x'][1])

    def _parse_unit_mode(self, match):
        if match['unit'] == 'MM':
            self.file_settings.unit = MM
        else:
            self.file_settings.unit = Inch

    def _parse_allegro_format_spec(self, match):
        self._parse_format_spec(match)
        self._parse_unit_mode(match)

    def _parse_load_polarity(self, match):
        self.graphics_state.polarity_dark = match['polarity'] == 'D'

    def _parse_offset(self, match):
        a, b = match['a'], match['b']
        a = float(a) if a else 0
        b = float(b) if b else 0
        self.graphics_state.offset = a, b

    def _parse_allegro_legacy_params(self, match):
        self._parse_image_rotation(match)
        self._parse_offset(match)
        self._parse_image_polarity(match)
        self._parse_mirror_image(match)
        self._parse_scale_factor(match)

    def _parse_include_file(self, match):
        if self.include_dir is None:
            self.warn('IF include statement found, but includes are deactivated.', ResourceWarning)
        else:
            self.warn('IF include statement found. Includes are activated, but is this really a good idea?', ResourceWarning)

        include_file = self.include_dir / param["filename"]
        # Do not check if path exists to avoid leaking existence via error message
        include_file = include_file.resolve(strict=False)
        
        if not include_file.is_relative_to(self.include_dir):
            raise FileNotFoundError('Attempted traversal to parent of include dir in path from IF include statement')

        if not include_file.is_file():
            raise FileNotFoundError('File pointed to by IF include statement does not exist')

        if include_file in self.include_stack:
            raise ValueError("Recusive inclusion via IF include statement.")
        self.include_stack.append(include_file)

        # Spec 2020-09 section 3.1: Gerber files must use UTF-8
        self._parse(f.read_text(encoding='UTF-8'), filename=include_file.name)
        self.include_stack.pop()

    def _parse_image_name(self, match):
        self.warn('Deprecated IN (image name) statement found. This deprecated since rev. I4 (Oct 2013).', DeprecationWarning)
        self.target.comments.append(f'Image name: {match["name"]}')

    def _parse_load_name(self, match):
        self.warn('Deprecated LN (load name) statement found. This deprecated since rev. I4 (Oct 2013).', DeprecationWarning)

    def _parse_axis_selection(self, match):
        if match['axes'] != 'AXBY':
            self.warn('Deprecated AS (axis selection) statement found. This deprecated since rev. I1 (Dec 2012).', DeprecationWarning)
        self.graphics_state.output_axes = match['axes']

    def _parse_image_polarity(self, match):
        polarity = dict(POS='positive', NEG='negative')[match['polarity']]
        if polarity != 'positive':
            self.warn('Deprecated IP (image polarity) statement found. This deprecated since rev. I4 (Oct 2013).', DeprecationWarning)
        self.graphics_state.image_polarity = polarity
    
    def _parse_image_rotation(self, match):
        rotation = int(match['rotation'])
        if rotation:
            self.warn('Deprecated IR (image rotation) statement found. This deprecated since rev. I1 (Dec 2012).', DeprecationWarning)
        self.graphics_state.image_rotation = rotation

    def _parse_mirror_image(self, match):
        mirror = bool(int(match['ma'] or '0')), bool(int(match['mb'] or '1'))
        if mirror != (False, False):
            self.warn('Deprecated MI (mirror image) statement found. This deprecated since rev. I1 (Dec 2012).', DeprecationWarning)
        self.graphics_state.image_mirror = mirror

    def _parse_scale_factor(self, match):
        a = float(match['sa']) if match['sa'] else 1.0
        b = float(match['sb']) if match['sb'] else 1.0
        if not math.isclose(math.dist((a, b), (1, 1)), 0):
            self.warn('Deprecated SF (scale factor) statement found. This deprecated since rev. I1 (Dec 2012).', DeprecationWarning)
        self.graphics_state.scale_factor = a, b

    def _parse_siemens_garbage(self, match):
        self.generator_hints.append('siemens')

    def _parse_comment(self, match):
        cmt = match["comment"].strip()

        # Parse metadata from allegro comments
        # We do this for layer identification since allegro files usually do not follow any defined naming scheme
        if cmt.startswith('File Origin:') and 'Allegro' in cmt:
            self.generator_hints.append('allegro')

        elif cmt.startswith('Layer:'):
            if 'BOARD GEOMETRY' in cmt:
                if 'SOLDERMASK_TOP' in cmt:
                    self.layer_hints.append('top mask')
                if 'SOLDERMASK_BOTTOM' in cmt:
                    self.layer_hints.append('bottom mask')
                if 'PASTEMASK_TOP' in cmt:
                    self.layer_hints.append('top paste')
                if 'PASTEMASK_BOTTOM' in cmt:
                    self.layer_hints.append('bottom paste')
                if 'SILKSCREEN_TOP' in cmt:
                    self.layer_hints.append('top silk')
                if 'SILKSCREEN_BOTTOM' in cmt:
                    self.layer_hints.append('bottom silk')
            elif 'ETCH' in cmt:
                _1, _2, name = cmt.partition('/')
                name = re.sub(r'\W+', '_', name)
                self.layer_hints.append(f'{name} copper')

        elif cmt.startswith('Mentor Graphics'):
            self.generator_hints.append('siemens')

        else:
            self.target.comments.append(cmt)

    def _parse_region_start(self, _match):
        self.current_region = go.Region(
                polarity_dark=self.graphics_state.polarity_dark,
                unit=self.file_settings.unit)

    def _parse_region_end(self, _match):
        if self.current_region is None:
            raise SyntaxError('Region end command (G37) outside of region')
        
        if self.current_region: # ignore empty regions
            self.target.objects.append(self.current_region)
        self.current_region = None

    def _parse_old_unit(self, match):
        self.file_settings.unit = Inch if match['mode'] == 'G70' else MM
        self.warn(f'Deprecated {match["mode"]} unit mode statement found. This deprecated since 2012.', DeprecationWarning)
        self.target.comments.append('Replaced deprecated {match["mode"]} unit mode statement with MO statement')

    def _parse_old_notation(self, match):
        # FIXME make sure we always have FS at end of processing.
        self.file_settings.notation = 'absolute' if match['mode'] == 'G90' else 'incremental'
        self.warn(f'Deprecated {match["mode"]} notation mode statement found. This deprecated since 2012.', DeprecationWarning)
        self.target.comments.append('Replaced deprecated {match["mode"]} notation mode statement with FS statement')

    def _parse_attribute(self, match):
        if match['type'] == 'TD':
            if match['value']:
                raise SyntaxError('TD attribute deletion command must not contain attribute fields')

            if not match['name']:
                self.object_attrs = {}
                self.aperture_attrs = {}
                return

            if match['name'] in self.file_attrs:
                raise SyntaxError('Attempt to TD delete file attribute. This does not make sense.')
            elif match['name'] in self.object_attrs:
                del self.object_attrs[match['name']]
            elif match['name'] in self.aperture_attrs:
                del self.aperture_attrs[match['name']]
            else:
                raise SyntaxError(f'Attempt to TD delete previously undefined attribute {match["name"]}.')

        else:
            target = {'TF': self.file_attrs, 'TO': self.object_attrs, 'TA': self.aperture_attrs}[match['type']]
            target[match['name']] = match['value'].split(',')

            if 'EAGLE' in self.file_attrs.get('.GenerationSoftware', []) or match['eagle_garbage']:
                self.generator_hints.append('eagle')
    
    def _parse_eof(self, _match):
        self.eof_found = True

    def _parse_ignored(self, match):
        pass

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('testfile')
    args = parser.parse_args()

    bounds = (0.0, 0.0), (6.0, 6.0) # bottom left, top right
    svg = str(GerberFile.open(args.testfile).to_svg(force_bounds=bounds, arg_unit='inch', fg='white', bg='black'))
    print(svg)

