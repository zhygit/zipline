"""
Utilities for validating inputs to user-facing API functions.
"""
import re
import sys
from textwrap import dedent
from types import CodeType
from functools import wraps
from inspect import getargspec as _real_getargspec
from uuid import uuid4

from toolz.curried.operator import getitem
from six import viewkeys, exec_, PY3


_code_argorder = (
    ('co_argcount', 'co_kwonlyargcount') if PY3 else ('co_argcount',)
) + (
    'co_nlocals',
    'co_stacksize',
    'co_flags',
    'co_code',
    'co_consts',
    'co_names',
    'co_varnames',
    'co_filename',
    'co_name',
    'co_firstlineno',
    'co_lnotab',
    'co_freevars',
    'co_cellvars',
)

MethodDescriptorType = type(str.split)

NO_DEFAULT = object()
CYTHON_SIGNATURE_PARSER = re.compile("^([\w_]+)\((.*)\)$")


def extract_argnames(sig):
    """
    Parse argument names from a Cython function signature.

    Example
    -------
    >>> remove_types_from_cython_function_signature("int x, int y")
    ['x', 'y']
    """
    if '=' in sig:
        raise SyntaxError(
            "Can't parse signatures containing default values: %r." % sig
        )
    argnames = []
    for piece in sig.split(','):
        # Function arguments in Cython can include a type before the argument,
        # so split on whitespace.
        sub_pieces = re.split(' *', piece.strip(' '))
        if len(sub_pieces) > 2:
            raise SyntaxError("Couldn't parse argument name from %r." % piece)
        argnames.append(sub_pieces[-1])

    return argnames


def parse_argspec_from_cython_embedsignature_line(signature_line):
    m = CYTHON_SIGNATURE_PARSER.match(signature_line)
    if m is None:
        raise SyntaxError("Can't parse signature line: %r." % signature_line)

    funcname, cy_sig = m.groups()
    argnames = extract_argnames(cy_sig)

    s = "def {funcname}({signature}): pass".format(
        funcname=funcname,
        signature=', '.join(argnames),
    )

    globals_ = {}
    locals_ = {}
    exec_(s, globals_, locals_)
    return _real_getargspec(locals_[funcname])


def safe_get_filename(func, default):
    """
    Get the name of the file in which a function was defined, falling back to
    default if no file could be found.
    """
    try:
        # This should work for most "normal" python functions.
        return func.__code__.co_filename
    except AttributeError:
        pass

    try:
        # C Extension functions don't have bytecode.  Try to find the file
        # associated with the file's __module__.
        return sys.modules[func.__module__].__file__
    except (AttributeError, KeyError):
        pass

    return default


def getargspec(f):
    """
    Enhanced version of inspect.getargspec that also works with Cython
    functions if they're created with `embedsignature=True`.
    """
    try:
        return _real_getargspec(f)
    except TypeError as e:
        # We need to reassign this in Py3 to use it below.
        first_error = e
        pass

    def fail(second_error):
        raise ValueError(
            "Couldn't get argspec for function {func}, and couldn't parse "
            "one from the docstring.\n\n"
            "Error from inspect.getargspec was: {first}.\n\n"
            "Subsequent error was: {second}.".format(
                func=f,
                first=first_error,
                second=second_error,
            )
        )

    try:
        cython_signature_line = f.__doc__.splitlines()[0]
        if f.__name__ + '(' not in cython_signature_line:
            raise ValueError(
                "First line of docstring was not a signature."
                "You may want to set cython.embedsignature=True."
            )
        return parse_argspec_from_cython_embedsignature_line(
            cython_signature_line,
        )
    except Exception as e:
        fail(e)


def preprocess(*_unused, **processors):
    """
    Decorator that applies pre-processors to the arguments of a function before
    calling the function.

    Parameters
    ----------
    **processors : dict
        Map from argument name -> processor function.

        A processor function takes three arguments: (func, argname, argvalue).

        `func` is the the function for which we're processing args.
        `argname` is the name of the argument we're processing.
        `argvalue` is the value of the argument we're processing.

    Usage
    -----
    >>> def _ensure_tuple(func, argname, arg):
    ...     if isinstance(arg, tuple):
    ...         return argvalue
    ...     try:
    ...         return tuple(arg)
    ...     except TypeError:
    ...         raise TypeError(
    ...             "%s() expected argument '%s' to"
    ...             " be iterable, but got %s instead." % (
    ...                 func.__name__, argname, arg,
    ...             )
    ...         )
    ...
    >>> @preprocess(arg=_ensure_tuple)
    ... def foo(arg):
    ...     return arg
    ...
    >>> foo([1, 2, 3])
    (1, 2, 3)
    >>> foo("a")
    ('a',)
    >>> foo(2)
    Traceback (most recent call last):
        ...
    TypeError: foo() expected argument 'arg' to be iterable, but got 2 instead.
    """
    if _unused:
        raise TypeError("preprocess() doesn't accept positional arguments")

    def _decorator(f):
        args, varargs, varkw, defaults = argspec = getargspec(f)
        if defaults is None:
            defaults = ()
        no_defaults = (NO_DEFAULT,) * (len(args) - len(defaults))
        args_defaults = zip(args, no_defaults + defaults)

        argset = set(args)

        # These assumptions simplify the implementation significantly.  If you
        # really want to validate a *args/**kwargs function, you'll have to
        # implement this here or do it yourself.
        if varargs:
            raise TypeError(
                "Can't validate functions that take *args: %s" % argspec
            )
        if varkw:
            raise TypeError(
                "Can't validate functions that take **kwargs: %s" % argspec
            )

        # Arguments can be declared as tuples in Python 2.
        if not all(isinstance(arg, str) for arg in args):
            raise TypeError(
                "Can't validate functions using tuple unpacking: %s" % argspec
            )

        # Ensure that all processors map to valid names.
        bad_names = viewkeys(processors) - argset
        if bad_names:
            raise TypeError(
                "Got processors for unknown arguments: %s." % bad_names
            )

        return _build_preprocessed_function(f, processors, args_defaults)
    return _decorator


def call(f):
    """
    Wrap a function in a processor that calls `f` on the argument before
    passing it along.

    Useful for creating simple arguments to the `@preprocess` decorator.

    Parameters
    ----------
    f : function
        Function accepting a single argument and returning a replacement.

    Usage
    -----
    >>> @preprocess(x=call(lambda x: x + 1))
    ... def foo(x):
    ...     return x
    ...
    >>> foo(1)
    2
    """
    @wraps(f)
    def processor(func, argname, arg):
        return f(arg)
    return processor


def _build_preprocessed_function(func, processors, args_defaults):
    """
    Build a preprocessed function with the same signature as `func`.

    Uses `exec` internally to build a function that actually has the same
    signature as `func.
    """
    format_kwargs = {'func_name': func.__name__}

    def mangle(name):
        return 'a' + uuid4().hex + name

    format_kwargs['mangled_func'] = mangled_funcname = mangle(func.__name__)

    def make_processor_assignment(arg, processor_name):
        template = "{arg} = {processor}({func}, '{arg}', {arg})"
        return template.format(
            arg=arg,
            processor=processor_name,
            func=mangled_funcname,
        )

    exec_globals = {mangled_funcname: func, 'wraps': wraps}
    defaults_seen = 0
    default_name_template = 'a' + uuid4().hex + '_%d'
    signature = []
    call_args = []
    assignments = []
    for arg, default in args_defaults:
        if default is NO_DEFAULT:
            signature.append(arg)
        else:
            default_name = default_name_template % defaults_seen
            exec_globals[default_name] = default
            signature.append('='.join([arg, default_name]))
            defaults_seen += 1

        if arg in processors:
            procname = mangle('_processor_' + arg)
            exec_globals[procname] = processors[arg]
            assignments.append(make_processor_assignment(arg, procname))

        call_args.append(arg + '=' + arg)

    exec_str = dedent(
        """\
        @wraps({wrapped_funcname})
        def {func_name}({signature}):
            {assignments}
            return {wrapped_funcname}({call_args})
        """
    ).format(
        func_name=func.__name__,
        signature=', '.join(signature),
        assignments='\n    '.join(assignments),
        wrapped_funcname=mangled_funcname,
        call_args=', '.join(call_args),
    )
    compiled = compile(
        exec_str,
        filename=safe_get_filename(func, default='<dynamically-generated>'),
        mode='exec',
    )

    exec_locals = {}
    exec_(compiled, exec_globals, exec_locals)
    new_func = exec_locals[func.__name__]

    code = new_func.__code__
    args = {
        attr: getattr(code, attr)
        for attr in dir(code)
        if attr.startswith('co_')
    }
    # Copy the firstlineno out of the underlying function so that exceptions
    # get raised with the correct traceback.
    # This also makes dynamic source inspection (like IPython `??` operator)
    # work as intended.
    try:
        # Try to get the pycode object from the underlying function.
        original_code = func.__code__
    except AttributeError:
        try:
            # The underlying callable was not a function, try to grab the
            # `__func__.__code__` which exists on method objects.
            original_code = func.__func__.__code__
        except AttributeError:
            # The underlying callable does not have a `__code__`. There is
            # nothing for us to correct.
            return new_func

    args['co_firstlineno'] = original_code.co_firstlineno
    new_func.__code__ = CodeType(*map(getitem(args), _code_argorder))
    return new_func
