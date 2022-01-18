#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2014 Hamilton Kibbe <ham@hamiltonkib.be>

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
gerber.utils
============
**Gerber and Excellon file handling utilities**

This module provides utility functions for working with Gerber and Excellon
files.
"""

import os
from math import radians, sin, cos, sqrt, atan2, pi


class LengthUnit:
    def __init__(self, name, shorthand, this_in_mm):
        self.name = name
        self.shorthand = shorthand
        self.factor = this_in_mm

    def convert_from(self, unit, value):
        if isinstance(unit, str):
            unit = units[unit]

        if unit == self or unit is None or value is None:
            return value

        return value * unit.factor / self.factor

    def convert_to(self, unit, value):
        if isinstance(unit, str):
            unit = to_unit(unit)

        if unit is None:
            return value

        return unit.convert_from(self, value)

    def __call__(self, value, unit):
        return self.convert_from(unit, value)

    def __eq__(self, other):
        if isinstance(other, str):
            return other.lower() in (self.name, self.shorthand)
        else:
            return id(self) == id(other)


MILLIMETERS_PER_INCH = 25.4
Inch = LengthUnit('inch', 'in', MILLIMETERS_PER_INCH)
MM = LengthUnit('millimeter', 'mm', 1)
units = {'inch': Inch, 'mm': MM, None: None}
to_unit = lambda name: units[name]


def decimal_string(value, precision=6, padding=False):
    """ Convert float to string with limited precision

    Parameters
    ----------
    value : float
        A floating point value.

    precision :
        Maximum number of decimal places to print

    Returns
    -------
    value : string
        The specified value as a  string.

    """
    floatstr = '%0.10g' % value
    integer = None
    decimal = None
    if '.' in floatstr:
        integer, decimal = floatstr.split('.')
    elif ',' in floatstr:
        integer, decimal = floatstr.split(',')
    else:
        integer, decimal = floatstr, "0"

    if len(decimal) > precision:
        decimal = decimal[:precision]
    elif padding:
        decimal = decimal + (precision - len(decimal)) * '0'

    if integer or decimal:
        return ''.join([integer, '.', decimal])
    else:
        return int(floatstr)

def validate_coordinates(position):
    if position is not None:
        if len(position) != 2:
            raise TypeError('Position must be a tuple (n=2) of coordinates')
        else:
            for coord in position:
                if not (isinstance(coord, int) or isinstance(coord, float)):
                    raise TypeError('Coordinates must be integers or floats')

def rotate_point(point, angle, center=(0.0, 0.0)):
    """ Rotate a point about another point.

    Parameters
    -----------
    point : tuple(<float>, <float>)
        Point to rotate about origin or center point

    angle : float
        Angle to rotate the point [degrees]

    center : tuple(<float>, <float>)
        Coordinates about which the point is rotated. Defaults to the origin.

    Returns
    -------
    rotated_point : tuple(<float>, <float>)
        `point` rotated about `center` by `angle` degrees.
    """
    angle = radians(angle)

    cos_angle = cos(angle)
    sin_angle = sin(angle)

    return (
            cos_angle * (point[0] - center[0]) - sin_angle * (point[1] - center[1]) + center[0],
            sin_angle * (point[0] - center[0]) + cos_angle * (point[1] - center[1]) + center[1])

def nearly_equal(point1, point2, ndigits = 6):
    '''Are the points nearly equal'''

    return round(point1[0] - point2[0], ndigits) == 0 and round(point1[1] - point2[1], ndigits) == 0


def sq_distance(point1, point2):

    diff1 = point1[0] - point2[0]
    diff2 = point1[1] - point2[1]
    return diff1 * diff1 + diff2 * diff2

