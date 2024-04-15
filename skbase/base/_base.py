# -*- coding: utf-8 -*-
# copyright: skbase developers, BSD-3-Clause License (see LICENSE file)
# Elements of BaseObject reuse code developed in scikit-learn. These elements
# are copyrighted by the scikit-learn developers, BSD-3-Clause License. For
# conditions see https://github.com/scikit-learn/scikit-learn/blob/main/COPYING
"""Base class template for objects and fittable objects.

templates in this module:

    BaseObject - object with parameters and tags
    BaseEstimator - BaseObject that can be fitted

Interface specifications below.

---

    class name: BaseObject

Parameter inspection and setter methods
    inspect parameter values      - get_params()
    setting parameter values      - set_params(**params)
    list of parameter names       - get_param_names()
    dict of parameter defaults    - get_param_defaults()

Tag inspection and setter methods
    inspect tags (all)            - get_tags()
    inspect tags (one tag)        - get_tag(tag_name: str, tag_value_default=None)
    inspect tags (class method)   - get_class_tags()
    inspect tags (one tag, class) - get_class_tag(tag_name:str, tag_value_default=None)
    setting dynamic tags          - set_tag(**tag_dict: dict)
    set/clone dynamic tags        - clone_tags(estimator, tag_names=None)

Blueprinting: resetting and cloning, post-init state with same hyper-parameters
    reset estimator to post-init  - reset()
    cloneestimator (copy&reset)   - clone()

Testing with default parameters methods
    getting default parameters (all sets)         - get_test_params()
    get one test instance with default parameters - create_test_instance()
    get list of all test instances plus name list - create_test_instances_and_names()
---

    class name: BaseEstimator

Provides all interface points of BaseObject, plus:

Parameter inspection:
    fitted parameter inspection - get_fitted_params()

State:
    fitted model/strategy   - by convention, any attributes ending in "_"
    fitted state flag       - is_fitted (property)
    fitted state check      - check_is_fitted (raises error if not is_fitted)
"""
import inspect
import re
import warnings
from collections import defaultdict
from copy import deepcopy
from typing import List

from skbase._exceptions import NotFittedError
from skbase.base._pretty_printing._object_html_repr import _object_html_repr
from skbase.base._tagmanager import _FlagManager

__author__: List[str] = ["fkiraly", "mloning", "RNKuhns", "tpvasconcelos"]
__all__: List[str] = ["BaseEstimator", "BaseObject"]


class BaseObject(_FlagManager):
    """Base class for parametric objects with sktime style tag interface.

    Extends scikit-learn's BaseEstimator to include sktime style interface for tags.
    """

    _config = {
        "display": "diagram",
        "print_changed_only": True,
        "check_clone": False,  # whether to execute validity checks in clone
        "clone_config": True,  # clone config values (True) or use defaults (False)
    }

    def __init__(self):
        """Construct BaseObject."""
        self._init_flags(flag_attr_name="_tags")
        self._init_flags(flag_attr_name="_config")
        super(BaseObject, self).__init__()

    def __eq__(self, other):
        """Equality dunder. Checks equal class and parameters.

        Returns True iff result of get_params(deep=False)
        results in equal parameter sets.

        Nested BaseObject descendants from get_params are compared via __eq__ as well.
        """
        from skbase.utils.deep_equals import deep_equals

        if not isinstance(other, BaseObject):
            return False

        self_params = self.get_params(deep=False)
        other_params = other.get_params(deep=False)

        return deep_equals(self_params, other_params)

    def __getattr__(self, attr):
        """Get attribute dunder, defaults to object tags if no attribute found.

        In tag names, the following characters are replaced:

        * colon by double underscore, i.e., ":": "__"
        * dash by single underscore, i.e., "-": "_"
        """
        # early stop for reserved attributes to avoid infinite recursion
        reserved_attr = attr.endswith("_dynamic")
        if reserved_attr:
            return object.__getattribute__(self, attr)

        # get tags and normalized keys
        tag_dict = self.get_tags()

        # if attribute is in tag_dict, return tag value
        if attr in tag_dict:
            return tag_dict[attr]

        # not found, now try normalized keys

        def norm_key(k):
            """Replace colon by double underscore, dash by single underscore."""
            return k.replace(":", "__", ).replace("-", "_")

        tag_dict_norm = {norm_key(k): v for k, v in tag_dict.items()}

        if attr in tag_dict_norm:
            return tag_dict_norm[attr]

        # otherwise raise the default AttributeError
        return object.__getattribute__(self, attr)

    def reset(self):
        """Reset the object to a clean post-init state.

        Using reset, runs __init__ with current values of hyper-parameters
        (result of get_params). This Removes any object attributes, except:

            - hyper-parameters = arguments of __init__
            - object attributes containing double-underscores, i.e., the string "__"

        Class and object methods, and class attributes are also unaffected.

        Returns
        -------
        self
            Instance of class reset to a clean post-init state but retaining
            the current hyper-parameter values.

        Notes
        -----
        Equivalent to sklearn.clone but overwrites self. After self.reset()
        call, self is equal in value to `type(self)(**self.get_params(deep=False))`
        """
        # retrieve parameters to copy them later
        params = self.get_params(deep=False)
        config = self.get_config()

        # delete all object attributes in self
        attrs = [attr for attr in dir(self) if "__" not in attr]
        cls_attrs = list(dir(type(self)))
        self_attrs = set(attrs).difference(cls_attrs)
        for attr in self_attrs:
            delattr(self, attr)

        # run init with a copy of parameters self had at the start
        self.__init__(**params)
        self.set_config(**config)

        return self

    def clone(self):
        """Obtain a clone of the object with same hyper-parameters.

        A clone is a different object without shared references, in post-init state.
        This function is equivalent to returning sklearn.clone of self.

        Raises
        ------
        RuntimeError if the clone is non-conforming, due to faulty ``__init__``.

        Notes
        -----
        If successful, equal in value to ``type(self)(**self.get_params(deep=False))``.
        """
        self_clone = _clone(self)
        if self.get_config()["check_clone"]:
            _check_clone(original=self, clone=self_clone)
        return self_clone

    @classmethod
    def _get_init_signature(cls):
        """Get class init signature.

        Useful in parameter inspection.

        Returns
        -------
        List
            The inspected parameter objects (including defaults).

        Raises
        ------
        RuntimeError if cls has varargs in __init__.
        """
        # fetch the constructor or the original constructor before
        # deprecation wrapping if any
        init = getattr(cls.__init__, "deprecated_original", cls.__init__)
        if init is object.__init__:
            # No explicit constructor to introspect
            return []

        # introspect the constructor arguments to find the model parameters
        # to represent
        init_signature = inspect.signature(init)

        # Consider the constructor parameters excluding 'self'
        parameters = [
            p
            for p in init_signature.parameters.values()
            if p.name != "self" and p.kind != p.VAR_KEYWORD
        ]
        for p in parameters:
            if p.kind == p.VAR_POSITIONAL:
                raise RuntimeError(
                    "scikit-learn compatible estimators should always "
                    "specify their parameters in the signature"
                    " of their __init__ (no varargs)."
                    " %s with constructor %s doesn't "
                    " follow this convention." % (cls, init_signature)
                )
        return parameters

    @classmethod
    def get_param_names(cls):
        """Get object's parameter names.

        Returns
        -------
        param_names: list[str]
            Alphabetically sorted list of parameter names of cls.
        """
        parameters = cls._get_init_signature()
        param_names = sorted([p.name for p in parameters])
        return param_names

    @classmethod
    def get_param_defaults(cls):
        """Get object's parameter defaults.

        Returns
        -------
        default_dict: dict[str, Any]
            Keys are all parameters of cls that have a default defined in __init__
            values are the defaults, as defined in __init__.
        """
        parameters = cls._get_init_signature()
        default_dict = {
            x.name: x.default for x in parameters if x.default != inspect._empty
        }
        return default_dict

    def get_params(self, deep=True):
        """Get a dict of parameters values for this object.

        Parameters
        ----------
        deep : bool, default=True
            Whether to return parameters of components.

            * If True, will return a dict of parameter name : value for this object,
              including parameters of components (= BaseObject-valued parameters).
            * If False, will return a dict of parameter name : value for this object,
              but not include parameters of components.

        Returns
        -------
        params : dict with str-valued keys
            Dictionary of parameters, paramname : paramvalue
            keys-value pairs include:

            * always: all parameters of this object, as via `get_param_names`
              values are parameter value for that key, of this object
              values are always identical to values passed at construction
            * if `deep=True`, also contains keys/value pairs of component parameters
              parameters of components are indexed as `[componentname]__[paramname]`
              all parameters of `componentname` appear as `paramname` with its value
            * if `deep=True`, also contains arbitrary levels of component recursion,
              e.g., `[componentname]__[componentcomponentname]__[paramname]`, etc
        """
        params = {key: getattr(self, key) for key in self.get_param_names()}

        if deep:
            deep_params = {}
            for key, value in params.items():
                if hasattr(value, "get_params"):
                    deep_items = value.get_params().items()
                    deep_params.update({f"{key}__{k}": val for k, val in deep_items})
            params.update(deep_params)

        return params

    def set_params(self, **params):
        """Set the parameters of this object.

        The method works on simple estimators as well as on composite objects.
        Parameter key strings ``<component>__<parameter>`` can be used for composites,
        i.e., objects that contain other objects, to access ``<parameter>`` in
        the component ``<component>``.
        The string ``<parameter>``, without ``<component>__``, can also be used if
        this makes the reference unambiguous, e.g., there are no two parameters of
        components with the name ``<parameter>``.

        Parameters
        ----------
        **params : dict
            BaseObject parameters, keys must be ``<component>__<parameter>`` strings.
            __ suffixes can alias full strings, if unique among get_params keys.

        Returns
        -------
        self : reference to self (after parameters have been set)
        """
        if not params:
            # Simple optimization to gain speed (inspect is slow)
            return self
        valid_params = self.get_params(deep=True)

        unmatched_keys = []

        nested_params = defaultdict(dict)  # grouped by prefix
        for full_key, value in params.items():
            # split full_key by first occurrence of __, if contains __
            # "key_without_dblunderscore" -> "key_without_dbl_underscore", None, None
            # "key__with__dblunderscore" -> "key", "__", "with__dblunderscore"
            key, delim, sub_key = full_key.partition("__")
            # if key not recognized, remember for suffix matching
            if key not in valid_params:
                unmatched_keys += [key]
            # if full_key contained __, collect suffix for component set_params
            elif delim:
                nested_params[key][sub_key] = value
            # if key is found and did not contain __, set self.key to the value
            else:
                setattr(self, key, value)
                valid_params[key] = value

        # all matched params have now been set
        # reset estimator to clean post-init state with those params
        self.reset()

        # recurse in components
        for key, sub_params in nested_params.items():
            valid_params[key].set_params(**sub_params)

        # for unmatched keys, resolve by aliasing via available __ suffixes, recurse
        if len(unmatched_keys) > 0:
            valid_params = self.get_params(deep=True)
            unmatched_params = {key: params[key] for key in unmatched_keys}

            # aliasing, syntactic sugar to access uniquely named params more easily
            aliased_params = self._alias_params(unmatched_params, valid_params)

            # if none of the parameter names change through aliasing, raise error
            if set(aliased_params) == set(unmatched_params):
                raise ValueError(
                    f"Invalid parameter keys provided to set_params of object {self}. "
                    "Check the list of available parameters "
                    "with `object.get_params().keys()`. "
                    f"Invalid keys provided: {unmatched_keys}"
                )

            # recurse: repeat matching and aliasing until no further matches found
            #   termination condition is above, "no change in keys via aliasing"
            self.set_params(**aliased_params)

        return self

    def _alias_params(self, d, valid_params):
        """Replace shorthands in d by full keys from valid_params.

        Parameters
        ----------
        d: dict with str keys
        valid_params: dict with str keys

        Result
        ------
        alias_dict: dict with str keys, all keys in valid_params
            values are as in d, with keys replaced by following rule:
            If key is a __ suffix of exactly one key in valid_params,
                it is replaced by that key. Otherwise an exception is raised.
            A __ suffix of a str is any str obtained as suffix from partition by __.
            Else, i.e., if key is in valid_params or not a __ suffix,
            the key is replaced by itself, i.e., left unchanged.

        Raises
        ------
        ValueError if at least one key of d is neither contained in valid_params,
            nor is it a __ suffix of exactly one key in valid_params
        """

        def _is_suffix(x, y):
            """Return whether x is a strict __ suffix of y."""
            return y.endswith(x) and y.endswith("__" + x)

        def _get_alias(x, d):
            """Return alias of x in d."""
            # if key is in valid_params, key is replaced by key (itself)
            if any(x == y for y in d.keys()):
                return x

            suff_list = [y for y in d.keys() if _is_suffix(x, y)]

            # if key is a __ suffix of exactly one key in valid_params,
            #   it is replaced by that key
            ns = len(suff_list)
            if ns > 1:
                raise ValueError(
                    f"suffix {x} does not uniquely determine parameter key, of "
                    f"{type(self).__name__} instance"
                    f"the following parameter keys have the same suffix: {suff_list}"
                )
            if ns == 0:
                return x
            # if ns == 1
            return suff_list[0]

        alias_dict = {_get_alias(x, valid_params): d[x] for x in d.keys()}

        return alias_dict

    @classmethod
    def get_class_tags(cls):
        """Get class tags from the class and all its parent classes.

        Retrieves tag: value pairs from _tags class attribute. Does not return
        information from dynamic tags (set via set_tags or clone_tags)
        that are defined on instances.

        Returns
        -------
        collected_tags : dict
            Dictionary of class tag name: tag value pairs. Collected from _tags
            class attribute via nested inheritance.
        """
        return cls._get_class_flags(flag_attr_name="_tags")

    @classmethod
    def get_class_tag(cls, tag_name, tag_value_default=None):
        """Get a class tag's value.

        Does not return information from dynamic tags (set via set_tags or clone_tags)
        that are defined on instances.

        Parameters
        ----------
        tag_name : str
            Name of tag value.
        tag_value_default : any
            Default/fallback value if tag is not found.

        Returns
        -------
        tag_value :
            Value of the `tag_name` tag in self. If not found, returns
            `tag_value_default`.
        """
        return cls._get_class_flag(
            flag_name=tag_name,
            flag_value_default=tag_value_default,
            flag_attr_name="_tags",
        )

    def get_tags(self):
        """Get tags from estimator class and dynamic tag overrides.

        Returns
        -------
        collected_tags : dict
            Dictionary of tag name : tag value pairs. Collected from _tags
            class attribute via nested inheritance and then any overrides
            and new tags from _tags_dynamic object attribute.
        """
        return self._get_flags(flag_attr_name="_tags")

    def get_tag(self, tag_name, tag_value_default=None, raise_error=True):
        """Get tag value from estimator class and dynamic tag overrides.

        Parameters
        ----------
        tag_name : str
            Name of tag to be retrieved
        tag_value_default : any type, optional; default=None
            Default/fallback value if tag is not found
        raise_error : bool
            whether a ValueError is raised when the tag is not found

        Returns
        -------
        tag_value : Any
            Value of the `tag_name` tag in self. If not found, returns an error if
            `raise_error` is True, otherwise it returns `tag_value_default`.

        Raises
        ------
        ValueError if raise_error is True i.e. if `tag_name` is not in
        self.get_tags().keys()
        """
        return self._get_flag(
            flag_name=tag_name,
            flag_value_default=tag_value_default,
            raise_error=raise_error,
            flag_attr_name="_tags",
        )

    def set_tags(self, **tag_dict):
        """Set dynamic tags to given values.

        Parameters
        ----------
        **tag_dict : dict
            Dictionary of tag name: tag value pairs.

        Returns
        -------
        Self
            Reference to self.

        Notes
        -----
        Changes object state by setting tag values in tag_dict as dynamic tags in self.
        """
        self._set_flags(flag_attr_name="_tags", **tag_dict)

        return self

    def clone_tags(self, estimator, tag_names=None):
        """Clone tags from another estimator as dynamic override.

        Parameters
        ----------
        estimator : estimator inheriting from :class:BaseEstimator
        tag_names : str or list of str, default = None
            Names of tags to clone. If None then all tags in estimator are used
            as `tag_names`.

        Returns
        -------
        Self :
            Reference to self.

        Notes
        -----
        Changes object state by setting tag values in tag_set from estimator as
        dynamic tags in self.
        """
        self._clone_flags(
            estimator=estimator, flag_names=tag_names, flag_attr_name="_tags"
        )

        return self

    def get_config(self):
        """Get config flags for self.

        Returns
        -------
        config_dict : dict
            Dictionary of config name : config value pairs. Collected from _config
            class attribute via nested inheritance and then any overrides
            and new tags from _onfig_dynamic object attribute.
        """
        return self._get_flags(flag_attr_name="_config")

    def set_config(self, **config_dict):
        """Set config flags to given values.

        Parameters
        ----------
        config_dict : dict
            Dictionary of config name : config value pairs.

        Returns
        -------
        self : reference to self.

        Notes
        -----
        Changes object state, copies configs in config_dict to self._config_dynamic.
        """
        self._set_flags(flag_attr_name="_config", **config_dict)

        return self

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        params_with_defaults = set(cls.get_param_defaults().keys())
        all_params = set(cls.get_param_names())
        params_without_defaults = all_params - params_with_defaults

        # if non-default parameters are required, but none have been found, raise error
        if len(params_without_defaults) > 0:
            raise ValueError(
                f"Estimator: {cls} has parameters without default values, "
                f"but these are not set in get_test_params. "
                f"Please set them in get_test_params, or provide default values. "
                f"Also see the respective extension template, if applicable."
            )

        # construct with parameter configuration for testing, otherwise construct with
        # default parameters (empty dict)
        params = {}
        return params

    @classmethod
    def create_test_instance(cls, parameter_set="default"):
        """Construct Estimator instance if possible.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        instance : instance of the class with default parameters

        Notes
        -----
        `get_test_params` can return dict or list of dict.
        This function takes first or single dict that get_test_params returns, and
        constructs the object with that.
        """
        if "parameter_set" in inspect.getfullargspec(cls.get_test_params).args:
            params = cls.get_test_params(parameter_set=parameter_set)
        else:
            params = cls.get_test_params()

        if isinstance(params, list) and isinstance(params[0], dict):
            params = params[0]
        elif isinstance(params, dict):
            pass
        else:
            raise TypeError(
                "get_test_params should either return a dict or list of dict."
            )

        return cls._safe_init_test_params(params)

    @classmethod
    def create_test_instances_and_names(cls, parameter_set="default"):
        """Create list of all test instances and a list of names for them.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        objs : list of instances of cls
            i-th instance is cls(**cls.get_test_params()[i])
        names : list of str, same length as objs
            i-th element is name of i-th instance of obj in tests
            convention is {cls.__name__}-{i} if more than one instance
            otherwise {cls.__name__}
        """
        if "parameter_set" in inspect.getfullargspec(cls.get_test_params).args:
            param_list = cls.get_test_params(parameter_set=parameter_set)
        else:
            param_list = cls.get_test_params()

        objs = []
        if not isinstance(param_list, (dict, list)):
            raise RuntimeError(
                f"Error in {cls.__name__}.get_test_params, "
                "return must be param dict for class, or list thereof"
            )
        if isinstance(param_list, dict):
            param_list = [param_list]
        for params in param_list:
            if not isinstance(params, dict):
                raise RuntimeError(
                    f"Error in {cls.__name__}.get_test_params, "
                    "return must be param dict for class, or list thereof"
                )
            objs += [cls._safe_init_test_params(params)]

        num_instances = len(param_list)
        if num_instances > 1:
            names = [cls.__name__ + "-" + str(i) for i in range(num_instances)]
        else:
            names = [cls.__name__]

        return objs, names

    @classmethod
    def _safe_init_test_params(cls, params):
        """Safe init of cls with params for testing.

        Will raise informative error message if params are not valid.
        """
        try:
            return cls(**params)
        except Exception as e:
            raise type(e)(
                f"Error in {cls.__name__}.get_test_params, "
                "return must be valid param dict for class, or list thereof, "
                "but attempted construction raised a exception. "
                f"Problematic parameter set: {params}. Exception raised: {e}"
            ) from e

    @classmethod
    def _has_implementation_of(cls, method):
        """Check if method has a concrete implementation in this class.

        This assumes that having an implementation is equivalent to
            one or more overrides of `method` in the method resolution order.

        Parameters
        ----------
        method : str, name of method to check implementation of

        Returns
        -------
        bool, whether method has implementation in cls
            True if cls.method has been overridden at least once in
                the inheritance tree (according to method resolution order)
        """
        # walk through method resolution order and inspect methods
        #   of classes and direct parents, "adjacent" classes in mro
        mro = inspect.getmro(cls)
        # collect all methods that are not none
        methods = [getattr(c, method, None) for c in mro]
        methods = [m for m in methods if m is not None]

        for i in range(len(methods) - 1):
            # the method has been overridden once iff
            #  at least two of the methods collected are not equal
            #  equivalently: some two adjacent methods are not equal
            overridden = methods[i] != methods[i + 1]
            if overridden:
                return True

        return False

    def is_composite(self):
        """Check if the object is composed of other BaseObjects.

        A composite object is an object which contains objects, as parameters.
        Called on an instance, since this may differ by instance.

        Returns
        -------
        composite: bool
            Whether an object has any parameters whose values
            are BaseObjects.
        """
        # walk through method resolution order and inspect methods
        #   of classes and direct parents, "adjacent" classes in mro
        params = self.get_params(deep=False)
        composite = any(isinstance(x, BaseObject) for x in params.values())

        return composite

    def _components(self, base_class=None):
        """Return references to all state changing BaseObject type attributes.

        This *excludes* the blue-print-like components passed in the __init__.

        Caution: this method returns *references* and not *copies*.
            Writing to the reference will change the respective attribute of self.

        Parameters
        ----------
        base_class : class, optional, default=None, must be subclass of BaseObject
            if not None, sub-sets return dict to only descendants of base_class

        Returns
        -------
        dict with key = attribute name, value = reference to that BaseObject attribute
        dict contains all attributes of self that inherit from BaseObjects, and:
            whose names do not contain the string "__", e.g., hidden attributes
            are not class attributes, and are not hyper-parameters (__init__ args)
        """
        if base_class is None:
            base_class = BaseObject
        if base_class is not None and not inspect.isclass(base_class):
            raise TypeError(f"base_class must be a class, but found {type(base_class)}")
        if base_class is not None and not issubclass(base_class, BaseObject):
            raise TypeError("base_class must be a subclass of BaseObject")

        # retrieve parameter names to exclude them later
        param_names = self.get_params(deep=False).keys()

        # retrieve all attributes that are BaseObject descendants
        attrs = [attr for attr in dir(self) if "__" not in attr]
        cls_attrs = list(dir(type(self)))
        self_attrs = set(attrs).difference(cls_attrs).difference(param_names)

        comp_dict = {x: getattr(self, x) for x in self_attrs}
        comp_dict = {x: y for (x, y) in comp_dict.items() if isinstance(y, base_class)}

        return comp_dict

    def __repr__(self, n_char_max: int = 700):
        """Represent class as string.

        This follows the scikit-learn implementation for the string representation
        of parameterized objects.

        Parameters
        ----------
        n_char_max : int
            Maximum (approximate) number of non-blank characters to render. This
            can be useful in testing.
        """
        from skbase.base._pretty_printing._pprint import _BaseObjectPrettyPrinter

        n_max_elements_to_show = 30  # number of elements to show in sequences
        # use ellipsis for sequences with a lot of elements
        pp = _BaseObjectPrettyPrinter(
            compact=True,
            indent=1,
            indent_at_name=True,
            n_max_elements_to_show=n_max_elements_to_show,
            changed_only=self.get_config()["print_changed_only"],
        )

        repr_ = pp.pformat(self)

        # Use bruteforce ellipsis when there are a lot of non-blank characters
        n_nonblank = len("".join(repr_.split()))
        if n_nonblank > n_char_max:
            lim = n_char_max // 2  # apprx number of chars to keep on both ends
            regex = r"^(\s*\S){%d}" % lim
            # The regex '^(\s*\S){%d}' matches from the start of the string
            # until the nth non-blank character:
            # - ^ matches the start of string
            # - (pattern){n} matches n repetitions of pattern
            # - \s*\S matches a non-blank char following zero or more blanks
            left_match = re.match(regex, repr_)
            right_match = re.match(regex, repr_[::-1])
            left_lim = left_match.end() if left_match is not None else 0
            right_lim = right_match.end() if right_match is not None else 0

            if "\n" in repr_[left_lim:-right_lim]:
                # The left side and right side aren't on the same line.
                # To avoid weird cuts, e.g.:
                # categoric...ore',
                # we need to start the right side with an appropriate newline
                # character so that it renders properly as:
                # categoric...
                # handle_unknown='ignore',
                # so we add [^\n]*\n which matches until the next \n
                regex += r"[^\n]*\n"
                right_match = re.match(regex, repr_[::-1])
                right_lim = right_match.end() if right_match is not None else 0

            ellipsis = "..."
            if left_lim + len(ellipsis) < len(repr_) - right_lim:
                # Only add ellipsis if it results in a shorter repr
                repr_ = repr_[:left_lim] + "..." + repr_[-right_lim:]

        return repr_

    @property
    def _repr_html_(self):
        """HTML representation of BaseObject.

        This is redundant with the logic of `_repr_mimebundle_`. The latter
        should be favorted in the long term, `_repr_html_` is only
        implemented for consumers who do not interpret `_repr_mimbundle_`.
        """
        if self.get_config()["display"] != "diagram":
            raise AttributeError(
                "_repr_html_ is only defined when the "
                "`display` configuration option is set to 'diagram'."
            )
        return self._repr_html_inner

    def _repr_html_inner(self):
        """Return HTML representation of class.

        This function is returned by the @property `_repr_html_` to make
        `hasattr(BaseObject, "_repr_html_") return `True` or `False` depending
        on `self.get_config()["display"]`.
        """
        return _object_html_repr(self)

    def _repr_mimebundle_(self, **kwargs):
        """Mime bundle used by jupyter kernels to display instances of BaseObject."""
        output = {"text/plain": repr(self)}
        if self.get_config()["display"] == "diagram":
            output["text/html"] = _object_html_repr(self)
        return output

    def set_random_state(self, random_state=None, deep=True, self_policy="copy"):
        """Set random_state pseudo-random seed parameters for self.

        Finds ``random_state`` named parameters via ``estimator.get_params``,
        and sets them to integers derived from ``random_state`` via ``set_params``.
        These integers are sampled from chain hashing via ``sample_dependent_seed``,
        and guarantee pseudo-random independence of seeded random generators.

        Applies to ``random_state`` parameters in ``estimator`` depending on
        ``self_policy``, and remaining component estimators
        if and only if ``deep=True``.

        Note: calls ``set_params`` even if ``self`` does not have a ``random_state``,
        or none of the components have a ``random_state`` parameter.
        Therefore, ``set_random_state`` will reset any ``scikit-base`` estimator,
        even those without a ``random_state`` parameter.

        Parameters
        ----------
        random_state : int, RandomState instance or None, default=None
            Pseudo-random number generator to control the generation of the random
            integers. Pass int for reproducible output across multiple function calls.

        deep : bool, default=True
            Whether to set the random state in sub-estimators.
            If False, will set only ``self``'s ``random_state`` parameter, if exists.
            If True, will set ``random_state`` parameters in sub-estimators as well.

        self_policy : str, one of {"copy", "keep", "new"}, default="copy"

            * "copy" : ``estimator.random_state`` is set to input ``random_state``
            * "keep" : ``estimator.random_state`` is kept as is
            * "new" : ``estimator.random_state`` is set to a new random state,
            derived from input ``random_state``, and in general different from it

        Returns
        -------
        self : reference to self
        """
        from skbase.utils.random_state import set_random_state

        return set_random_state(
            self,
            random_state=random_state,
            deep=deep,
            root_policy=self_policy,
        )


class TagAliaserMixin:
    """Mixin class for tag aliasing and deprecation of old tags.

    To deprecate tags, add the TagAliaserMixin to BaseObject or BaseEstimator.
    alias_dict contains the deprecated tags, and supports removal and renaming.
        For removal, add an entry "old_tag_name": ""
        For renaming, add an entry "old_tag_name": "new_tag_name"
    deprecate_dict contains the version number of renaming or removal.
        the keys in deprecate_dict should be the same as in alias_dict.
        values in deprecate_dict should be strings, the version of removal/renaming.

    The class will ensure that new tags alias old tags and vice versa, during
    the deprecation period. Informative warnings will be raised whenever the
    deprecated tags are being accessed.

    When removing tags, ensure to remove the removed tags from this class.
    If no tags are deprecated anymore (e.g., all deprecated tags are removed/renamed),
    ensure toremove this class as a parent of BaseObject or BaseEstimator.
    """

    # dictionary of aliases
    # key = old tag; value = new tag, aliased by old tag
    # override this in a child class
    alias_dict = {"old_tag": "new_tag", "tag_to_remove": ""}

    # dictionary of removal version
    # key = old tag; value = version in which tag will be removed, as string
    deprecate_dict = {"old_tag": "0.12.0", "tag_to_remove": "99.99.99"}

    def __init__(self):
        """Construct TagAliaserMixin."""
        super(TagAliaserMixin, self).__init__()

    @classmethod
    def get_class_tags(cls):
        """Get class tags from estimator class and all its parent classes.

        Returns
        -------
        collected_tags : dict
            Dictionary of tag name : tag value pairs. Collected from _tags
            class attribute via nested inheritance. NOT overridden by dynamic
            tags set by set_tags or mirror_tags.
        """
        collected_tags = super(TagAliaserMixin, cls).get_class_tags()
        collected_tags = cls._complete_dict(collected_tags)
        return collected_tags

    @classmethod
    def get_class_tag(cls, tag_name, tag_value_default=None):
        """Get tag value from estimator class (only class tags).

        Parameters
        ----------
        tag_name : str
            Name of tag value.
        tag_value_default : any type
            Default/fallback value if tag is not found.

        Returns
        -------
        tag_value :
            Value of the `tag_name` tag in self. If not found, returns
            `tag_value_default`.
        """
        cls._deprecate_tag_warn([tag_name])
        return super(TagAliaserMixin, cls).get_class_tag(
            tag_name=tag_name, tag_value_default=tag_value_default
        )

    def get_tags(self):
        """Get tags from estimator class and dynamic tag overrides.

        Returns
        -------
        collected_tags : dict
            Dictionary of tag name : tag value pairs. Collected from _tags
            class attribute via nested inheritance and then any overrides
            and new tags from _tags_dynamic object attribute.
        """
        collected_tags = super(TagAliaserMixin, self).get_tags()
        collected_tags = self._complete_dict(collected_tags)
        return collected_tags

    def get_tag(self, tag_name, tag_value_default=None, raise_error=True):
        """Get tag value from estimator class and dynamic tag overrides.

        Parameters
        ----------
        tag_name : str
            Name of tag to be retrieved
        tag_value_default : any type, optional; default=None
            Default/fallback value if tag is not found
        raise_error : bool
            whether a ValueError is raised when the tag is not found

        Returns
        -------
        tag_value :
            Value of the `tag_name` tag in self. If not found, returns an error if
            raise_error is True, otherwise it returns `tag_value_default`.

        Raises
        ------
        ValueError if raise_error is True i.e. if tag_name is not in self.get_tags(
        ).keys()
        """
        self._deprecate_tag_warn([tag_name])
        return super(TagAliaserMixin, self).get_tag(
            tag_name=tag_name,
            tag_value_default=tag_value_default,
            raise_error=raise_error,
        )

    def set_tags(self, **tag_dict):
        """Set dynamic tags to given values.

        Parameters
        ----------
        tag_dict : dict
            Dictionary of tag name : tag value pairs.

        Returns
        -------
        Self :
            Reference to self.

        Notes
        -----
        Changes object state by setting tag values in tag_dict as dynamic tags
        in self.
        """
        self._deprecate_tag_warn(tag_dict.keys())

        tag_dict = self._complete_dict(tag_dict)
        super(TagAliaserMixin, self).set_tags(**tag_dict)
        return self

    @classmethod
    def _complete_dict(cls, tag_dict):
        """Add all aliased and aliasing tags to the dictionary."""
        alias_dict = cls.alias_dict
        deprecated_tags = set(tag_dict.keys()).intersection(alias_dict.keys())
        new_tags = set(tag_dict.keys()).intersection(alias_dict.values())

        if len(deprecated_tags) > 0 or len(new_tags) > 0:
            new_tag_dict = deepcopy(tag_dict)
            # for all tag strings being set, write the value
            #   to all tags that could *be aliased by* the string
            #   and all tags that could be *aliasing* the string
            # this way we ensure upwards and downwards compatibility
            for old_tag, new_tag in alias_dict.items():
                for tag in tag_dict:
                    if tag == old_tag and new_tag != "":
                        new_tag_dict[new_tag] = tag_dict[tag]
                    if tag == new_tag:
                        new_tag_dict[old_tag] = tag_dict[tag]
            return new_tag_dict
        else:
            return tag_dict

    @classmethod
    def _deprecate_tag_warn(cls, tags):
        """Print warning message for tag deprecation.

        Parameters
        ----------
        tags : list of str

        Raises
        ------
        DeprecationWarning for each tag in tags that is aliased by cls.alias_dict
        """
        for tag_name in tags:
            if tag_name in cls.alias_dict.keys():
                version = cls.deprecate_dict[tag_name]
                new_tag = cls.alias_dict[tag_name]
                msg = f"tag {tag_name!r} will be removed in sktime version {version}"
                if new_tag != "":
                    msg += (
                        f" and replaced by {new_tag!r}, please use {new_tag!r} instead"
                    )
                else:
                    msg += ", please remove code that access or sets {tag_name!r}"
                warnings.warn(msg, category=DeprecationWarning, stacklevel=2)


class BaseEstimator(BaseObject):
    """Base class for estimators with scikit-learn and sktime design patterns.

    Extends BaseObject to include basic functionality for fittable estimators.
    """

    # tuple of non-BaseObject classes that count as nested objects
    # get_fitted_params will retrieve parameters from these, too
    # override in descendant class - common choice: BaseEstimator from sklearn
    GET_FITTED_PARAMS_NESTING = ()

    def __init__(self):
        """Construct BaseEstimator."""
        self._is_fitted = False
        super(BaseEstimator, self).__init__()

    @property
    def is_fitted(self):
        """Whether `fit` has been called.

        Inspects object's `_is_fitted` attribute that should initialize to False
        during object construction, and be set to True in calls to an object's
        `fit` method.

        Returns
        -------
        bool
            Whether the estimator has been `fit`.
        """
        return self._is_fitted

    def check_is_fitted(self):
        """Check if the estimator has been fitted.

        Inspects object's `_is_fitted` attribute that should initialize to False
        during object construction, and be set to True in calls to an object's
        `fit` method.

        Raises
        ------
        NotFittedError
            If the estimator has not been fitted yet.
        """
        if not self.is_fitted:
            raise NotFittedError(
                f"This instance of {self.__class__.__name__} has not been fitted yet. "
                f"Please call `fit` first."
            )

    def get_fitted_params(self, deep=True):
        """Get fitted parameters.

        State required:
            Requires state to be "fitted".

        Parameters
        ----------
        deep : bool, default=True
            Whether to return fitted parameters of components.

            * If True, will return a dict of parameter name : value for this object,
              including fitted parameters of fittable components
              (= BaseEstimator-valued parameters).
            * If False, will return a dict of parameter name : value for this object,
              but not include fitted parameters of components.

        Returns
        -------
        fitted_params : dict with str-valued keys
            Dictionary of fitted parameters, paramname : paramvalue
            keys-value pairs include:

            * always: all fitted parameters of this object, as via `get_param_names`
              values are fitted parameter value for that key, of this object
            * if `deep=True`, also contains keys/value pairs of component parameters
              parameters of components are indexed as `[componentname]__[paramname]`
              all parameters of `componentname` appear as `paramname` with its value
            * if `deep=True`, also contains arbitrary levels of component recursion,
              e.g., `[componentname]__[componentcomponentname]__[paramname]`, etc
        """
        if not self.is_fitted:
            raise NotFittedError(
                f"estimator of type {type(self).__name__} has not been "
                "fitted yet, please call fit on data before get_fitted_params"
            )

        # collect non-nested fitted params of self
        fitted_params = self._get_fitted_params()

        # the rest is only for nested parameters
        # so, if deep=False, we simply return here
        if not deep:
            return fitted_params

        def sh(x):
            """Shorthand to remove all underscores at end of a string."""
            if x.endswith("_"):
                return sh(x[:-1])
            else:
                return x

        # add all nested parameters from components that are skbase BaseEstimator
        c_dict = self._components()
        for c, comp in c_dict.items():
            if isinstance(comp, BaseEstimator) and comp._is_fitted:
                c_f_params = comp.get_fitted_params(deep=deep)
                c_f_params = {f"{sh(c)}__{k}": v for k, v in c_f_params.items()}
                fitted_params.update(c_f_params)

        # add all nested parameters from components that are sklearn estimators
        # we do this recursively as we have to reach into nested sklearn estimators
        any_components_left_to_process = True
        old_new_params = fitted_params
        # this loop recursively and iteratively processes components inside components
        while any_components_left_to_process:
            new_params = {}
            for c, comp in old_new_params.items():
                if isinstance(comp, self.GET_FITTED_PARAMS_NESTING):
                    c_f_params = self._get_fitted_params_default(comp)
                    c_f_params = {f"{sh(c)}__{k}": v for k, v in c_f_params.items()}
                    new_params.update(c_f_params)
            fitted_params.update(new_params)
            old_new_params = new_params.copy()
            n_new_params = len(new_params)
            any_components_left_to_process = n_new_params > 0

        return fitted_params

    def _get_fitted_params_default(self, obj=None):
        """Obtain fitted params of object, per sklearn convention.

        Extracts a dict with {paramstr : paramvalue} contents,
        where paramstr are all string names of "fitted parameters".

        A "fitted attribute" of obj is one that ends in "_" but does not start with "_".
        "fitted parameters" are names of fitted attributes, minus the "_" at the end.

        Parameters
        ----------
        obj : any object, optional, default=self

        Returns
        -------
        fitted_params : dict with str keys
            fitted parameters, keyed by names of fitted parameter
        """
        obj = obj if obj else self

        # default retrieves all self attributes ending in "_"
        # and returns them with keys that have the "_" removed
        #
        # get all attributes ending in "_", exclude any that start with "_" (private)
        fitted_params = [
            attr for attr in dir(obj) if attr.endswith("_") and not attr.startswith("_")
        ]
        # remove the "_" at the end
        fitted_param_dict = {
            p[:-1]: getattr(obj, p) for p in fitted_params if hasattr(obj, p)
        }

        return fitted_param_dict

    def _get_fitted_params(self):
        """Get fitted parameters.

        private _get_fitted_params, called from get_fitted_params

        State required:
            Requires state to be "fitted".

        Returns
        -------
        fitted_params : dict with str keys
            fitted parameters, keyed by names of fitted parameter
        """
        return self._get_fitted_params_default()


# Adapted from sklearn's `_clone_parametrized()`
def _clone(estimator, *, safe=True):
    """Construct a new unfitted estimator with the same parameters.

    Clone does a deep copy of the model in an estimator
    without actually copying attached data. It returns a new estimator
    with the same parameters that has not been fitted on any data.

    Parameters
    ----------
    estimator : {list, tuple, set} of estimator instance or a single \
            estimator instance
        The estimator or group of estimators to be cloned.
    safe : bool, default=True
        If safe is False, clone will fall back to a deep copy on objects
        that are not estimators.

    Returns
    -------
    estimator : object
        The deep copy of the input, an estimator if input is an estimator.

    Notes
    -----
    If the estimator's `random_state` parameter is an integer (or if the
    estimator doesn't have a `random_state` parameter), an *exact clone* is
    returned: the clone and the original estimator will give the exact same
    results. Otherwise, *statistical clone* is returned: the clone might
    return different results from the original estimator. More details can be
    found in :ref:`randomness`.
    """
    estimator_type = type(estimator)
    # XXX: not handling dictionaries
    if estimator_type in (list, tuple, set, frozenset):
        return estimator_type([_clone(e, safe=safe) for e in estimator])
    elif not hasattr(estimator, "get_params") or isinstance(estimator, type):
        if not safe:
            return deepcopy(estimator)
        else:
            if isinstance(estimator, type):
                raise TypeError(
                    "Cannot clone object. "
                    + "You should provide an instance of "
                    + "scikit-learn estimator instead of a class."
                )
            else:
                raise TypeError(
                    "Cannot clone object '%s' (type %s): "
                    "it does not seem to be a scikit-learn "
                    "estimator as it does not implement a "
                    "'get_params' method." % (repr(estimator), type(estimator))
                )

    klass = estimator.__class__
    new_object_params = estimator.get_params(deep=False)
    for name, param in new_object_params.items():
        new_object_params[name] = _clone(param, safe=False)
    new_object = klass(**new_object_params)
    params_set = new_object.get_params(deep=False)

    # quick sanity check of the parameters of the clone
    for name in new_object_params:
        param1 = new_object_params[name]
        param2 = params_set[name]
        if param1 is not param2:
            raise RuntimeError(
                "Cannot clone object %s, as the constructor "
                "either does not set or modifies parameter %s" % (estimator, name)
            )

    # This is an extension to the original sklearn implementation
    if isinstance(estimator, BaseObject) and estimator.get_config()["clone_config"]:
        new_object.set_config(**estimator.get_config())

    return new_object


def _check_clone(original, clone):
    from skbase.utils.deep_equals import deep_equals

    self_params = original.get_params(deep=False)

    # check that all attributes are written to the clone
    for attrname in self_params.keys():
        if not hasattr(clone, attrname):
            raise RuntimeError(
                f"error in {original}.clone, __init__ must write all arguments "
                f"to self and not mutate them, but {attrname} was not found. "
                f"Please check __init__ of {original}."
            )

    clone_attrs = {attr: getattr(clone, attr) for attr in self_params.keys()}

    # check equality of parameters post-clone and pre-clone
    clone_attrs_valid, msg = deep_equals(self_params, clone_attrs, return_msg=True)
    if not clone_attrs_valid:
        raise RuntimeError(
            f"error in {original}.clone, __init__ must write all arguments "
            f"to self and not mutate them, but this is not the case. "
            f"Error on equality check of arguments (x) vs parameters (y): {msg}"
        )
