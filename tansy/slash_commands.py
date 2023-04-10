import asyncio
import copy
import functools
import inspect
import typing
from builtins import getattr

import attrs
import interactions as ipy

from . import slash_param
from . import utils

__all__ = (
    "TansySlashCommandParameter",
    "TansySlashCommand",
    "tansy_slash_command",
    "tansy_subcommand",
    "SlashCommand",
    "slash_command",
    "subcommand",
)


def _get_converter(anno: type, name: str):
    if typing.get_origin(anno) == typing.Annotated:
        anno = utils.get_from_anno_type(anno)

    if isinstance(anno, ipy.Converter):
        return ipy.BaseCommand._get_converter_function(anno, name)
    elif inspect.isroutine(anno):
        if hasattr(anno, "__code__"):
            num_params: int = anno.__code__.co_argcount
        else:
            num_params = len(inspect.signature(anno).parameters.values())

        match num_params:
            case 2:
                return anno
            case 1:

                async def _one_arg_convert(_, arg) -> typing.Any:
                    return await ipy.utils.maybe_coroutine(anno, arg)

                return _one_arg_convert
            case 0:
                raise ValueError(
                    f"{ipy.utils.get_object_name(anno)} for {name} has 0"
                    " positional arguments, which is unsupported."
                )
            case _:
                raise ValueError(
                    f"{ipy.utils.get_object_name(anno)} for {name} has more than 2"
                    " positional arguments, which is unsupported."
                )
    else:
        return None


_C = typing.TypeVar("_C", bound=typing.Callable)


def _overwrite_defaults(
    func: _C,
    defaults: dict[str, typing.Any],
    parameters: typing.Mapping[str, inspect.Parameter],
) -> _C:
    """
    A cursed piece of code that overrides the defaults in a function with the defaults
    provided, and returns the result of that. The defaults provided are assumed to be
    in the order that appear in the function. To be somewhat safe, the edited function
    is a shallow copy of the old function.

    The code here isn't very complex, and you can likely understand most of this from
    a glance, but this is a BAD idea, and you shouldn't do or use this as a beginner.
    Editing the raw defaults and kwdefaults can lead to unintended behavior,
    and touching magic properties like these are a big no-no in Python.
    Use `functools.partial` if you want to set your own defaults for a function
    programmatically most of the time.

    So why did I do it anyway?
    - Speed. Using `functools.partial` adds a pretty decent amount of overhead, especially
      if it has to combine two kwargs together. In Tansy, doing this instead of adding
      extra code in `call_callable` to insert defaults if they are missing adds an even
      greater speed benefit, as calculations for what is missing do not need to be done.

    - Compatibility and ease-of-use. If you're using the raw callback of commands for something,
      like how I do sometimes, you don't want to be tripped up by putting an argument as
      a positional argument instead of a keyword argument and suddenly getting an error,
      even if that would fine in the raw function itself.

    For example, this would occur:
    ```python
    async def original_func(arg: str):
        print(arg)

    defaults = {"arg": "hi!"}
    new_func = partial(original_func, **defaults)

    # would work fine in original_func, but python thinks that two values are being passed
    # for "arg" because kwarg vs. positional, causing an error that would be hard to understand
    # as an end-user
    await new_func("hey!")
    ```

    Technically, it is possible to make a wrapper around a function that would handle those
    cases just fine, but that adds a lot of overhead, more than just using `partial` or doing this.
    """
    func_copy = copy.copy(func)
    func_to_parse = func_copy

    partial_func = False

    if isinstance(func_copy, functools.partial):
        func_to_parse = func_copy.func
        partial_func = True

    old_kwarg_defaults = func_to_parse.__kwdefaults__ or {}

    new_defaults = []
    new_kwarg_defaults = {}

    for name, default in defaults.items():
        if (
            old_kwarg_defaults.get(name)
            or parameters[name].kind == inspect._ParameterKind.KEYWORD_ONLY
        ):
            new_kwarg_defaults[name] = default
        else:
            new_defaults.append(default)

    func_to_parse.__defaults__ = tuple(new_defaults) if new_defaults else None
    func_to_parse.__kwdefaults__ = new_kwarg_defaults or None

    if partial_func:
        func_copy = functools.partial(
            func_to_parse, *func_copy.args, **func_copy.keywords
        )
    else:
        func_copy = func_to_parse

    return func_copy


@attrs.define(slots=True)
class TansySlashCommandParameter:
    """An object representing parameters in a command."""

    name: str = attrs.field(default=None)
    argument_name: str = attrs.field(default=None)
    default: typing.Any = attrs.field(default=ipy.MISSING)
    type: typing.Type = attrs.field(default=None)
    converter: typing.Optional[typing.Callable] = attrs.field(default=None)

    @property
    def optional(self) -> bool:
        return self.default != ipy.MISSING


@attrs.define(eq=False, order=False, hash=False, kw_only=True)
class TansySlashCommand(ipy.SlashCommand):
    parameters: dict[str, TansySlashCommandParameter] = attrs.field(
        factory=dict, metadata=ipy.utils.no_export_meta
    )
    _inspect_signature: typing.Optional[inspect.Signature] = attrs.field(
        repr=False, default=None, metadata=ipy.utils.no_export_meta
    )

    def _overwrite_with_parameters(self):
        # i wont lie to you - what we're about to do is probably the
        # most disgusting, hacky thing ive done in python, but there's a good
        # reason for it
        #
        # you know how Option() exists in this lib? you know how you have to
        # do arg: type = Option() in order to define an option usually when
        # using tansy commands?
        # well, now Option() is the default of arg in the command, which
        # means if no value is provided for arg while using the raw callback,
        # instead of erroring out or receiving the value specified in default=X
        # (or None, if you used Optional and didn't explictly set a default value),
        # the function will instead just pass in the ParamInfo generated by Option(),
        # which is unintuitive and would result in a lot of bugs
        #
        # to prevent this, we overwrite the defaults in the function with ones
        # that make more sense considering tansy's features
        # explainations about the cursed _overwrite_defaults can be found
        # in the function itself

        defaults = {
            p.argument_name: p.default for p in self.parameters.values() if p.optional
        }
        self.callback = _overwrite_defaults(
            self.callback, defaults, self._inspect_signature.parameters
        )

    def _parse_parameters(self):
        if self.callback is None:
            return

        if self.parameters:
            self._overwrite_with_parameters()
            return

        self.options = []

        if not self._inspect_signature:
            if self.has_binding:
                callback = functools.partial(self.callback, None, None)
            else:
                callback = functools.partial(self.callback, None)

            self._inspect_signature = inspect.signature(callback)

        describes: dict[str, ipy.LocalisedDesc] = getattr(
            self.callback, "__tansy_describe__", {}
        )

        for param in self._inspect_signature.parameters.values():
            if param.kind == param.VAR_KEYWORD:
                # something like **kwargs, that's fine so let it pass
                continue

            if param.kind not in {
                param.POSITIONAL_OR_KEYWORD,
                param.KEYWORD_ONLY,
            }:
                raise ValueError(
                    "All parameters must be able to be used via keyword arguments."
                )

            cmd_param = TansySlashCommandParameter()
            param_info = (
                param.default
                if isinstance(param.default, slash_param.ParamInfo)
                else None
            )

            if param_info:
                option = param_info.generate_option()
            else:
                try:
                    option_type = utils.get_option(param.annotation)
                except ValueError:
                    raise ValueError(
                        f"Invalid/no provided type for {param.name}"
                    ) from None
                option = ipy.SlashCommandOption(name=param.name, type=option_type)

            cmd_param.name = str(option.name) if option.name else param.name
            cmd_param.argument_name = param.name
            option.name = option.name or ipy.LocalisedName.converter(cmd_param.name)

            if desc := describes.get(option.name.default):
                option.description = desc

            if option.type is None:
                try:
                    option.type = utils.get_option(param.annotation)
                except ValueError:
                    raise ValueError(
                        f"Invalid/no provided type for {param.name}"
                    ) from None

            if param_info:
                cmd_param.default = param_info.default
            elif param.default is not param.empty:
                option.required = False
                cmd_param.default = param.default
            else:
                cmd_param.default = ipy.MISSING

            # what we're checking here is:
            # - if we don't already have a default
            # - if the user didn't already specify a type in
            #   param_info that would indicate if its optional or not
            # - if the annotation is marked as optional
            # if so, we want to make the option not required, and the default be None
            if (
                cmd_param.default is ipy.MISSING
                and (not param_info or not param_info._user_provided_type)
                and utils.is_optional(param.annotation)
            ):
                option.required = False
                cmd_param.default = None

            if (
                param_info
                and option.type == ipy.OptionType.CHANNEL
                and not option.channel_types
            ):
                option.channel_types = utils.resolve_channel_types(param.annotation)  # type: ignore

            if param_info and param_info.converter:
                if convert_func := _get_converter(param_info.converter, param.name):
                    cmd_param.converter = convert_func
                else:
                    raise ValueError(
                        f"The converter for {param.name} is invalid. Please make"
                        " sure it is either a Converter-like class or a function."
                    )
            elif converter := _get_converter(param.annotation, param.name):
                cmd_param.converter = converter

            # we bypassed validation earlier, so let's make sure everything's okay
            # since we got the final option stuff now
            attrs.validate(option)  # type: ignore
            self.options.append(option)
            self.parameters[cmd_param.name] = cmd_param

        # make sure the options arent in an invalid order -
        # both to safeguard against invalid slash commands and because
        # we rely on optional arguments being after required arguments right after this
        attrs.validate(self)  # type: ignore
        self._overwrite_with_parameters()

    async def call_callback(
        self, callback: typing.Callable, ctx: ipy.InteractionContext
    ) -> None:
        """
        Runs the callback of this command.
        Args:
            callback (Callable: The callback to run. This is provided for compatibility with ipy.
            ctx (ipy.InteractionContext): The context to use for this command.
        """
        if not self.parameters:
            return await callback(ctx, **ctx.kwargs)

        new_kwargs = {}

        for key, value in ctx.kwargs.items():
            param = self.parameters.get(key)
            if not param:
                # hopefully you have **kwargs
                new_kwargs[key] = value
                continue

            if param.converter:
                converted = await ipy.utils.maybe_coroutine(param.converter, ctx, value)
            else:
                converted = value
            new_kwargs[param.argument_name] = converted

        return await self.call_with_binding(callback, ctx, **new_kwargs)

    def group(
        self,
        name: str = None,
        description: str = "No Description Set",
        inherit_checks: bool = True,
    ) -> "TansySlashCommand":
        return TansySlashCommand(
            name=self.name,
            description=self.description,
            group_name=name,
            group_description=description,
            scopes=self.scopes,
            checks=self.checks if inherit_checks else [],
        )

    def subcommand(
        self,
        sub_cmd_name: ipy.LocalisedName | str,
        group_name: ipy.LocalisedName | str = None,
        sub_cmd_description: ipy.Absent[ipy.LocalisedDesc | str] = ipy.MISSING,
        group_description: ipy.Absent[ipy.LocalisedDesc | str] = ipy.MISSING,
        nsfw: bool = False,
        inherit_checks: bool = True,
    ) -> typing.Callable[..., "TansySlashCommand"]:
        def wrapper(
            call: typing.Callable[..., typing.Coroutine]
        ) -> "TansySlashCommand":
            nonlocal sub_cmd_description

            if not asyncio.iscoroutinefunction(call):
                raise TypeError("Subcommand must be coroutine")

            if sub_cmd_description is ipy.MISSING:
                sub_cmd_description = call.__doc__ or "No Description Set"

            return TansySlashCommand(
                name=self.name,
                description=self.description,
                group_name=group_name or self.group_name,
                group_description=group_description or self.group_description,
                sub_cmd_name=sub_cmd_name,
                sub_cmd_description=sub_cmd_description,
                default_member_permissions=self.default_member_permissions,
                dm_permission=self.dm_permission,
                callback=call,
                scopes=self.scopes,
                nsfw=nsfw,
                checks=self.checks if inherit_checks else [],
            )

        return wrapper


def tansy_slash_command(
    name: str | ipy.LocalisedName,
    *,
    description: ipy.Absent[str | ipy.LocalisedDesc] = ipy.MISSING,
    scopes: ipy.Absent[typing.List["ipy.Snowflake_Type"]] = ipy.MISSING,
    default_member_permissions: typing.Optional["ipy.Permissions"] = None,
    dm_permission: bool = True,
    sub_cmd_name: str | ipy.LocalisedName = None,
    group_name: str | ipy.LocalisedName = None,
    sub_cmd_description: str | ipy.LocalisedDesc = "No Description Set",
    group_description: str | ipy.LocalisedDesc = "No Description Set",
    nsfw: bool = False,
) -> typing.Callable[[ipy.const.AsyncCallable], TansySlashCommand]:
    """
    A decorator to declare a coroutine as a Tansy slash command.
    note:
        While the base and group descriptions arent visible in the discord client, currently.
        We strongly advise defining them anyway, if you're using subcommands, as Discord has said they will be visible in
        one of the future ui updates.
    Args:
        name: 1-32 character name of the command
        description: 1-100 character description of the command
        scopes: The scope this command exists within
        default_member_permissions: What permissions members need to have by default to use this command.
        dm_permission: Should this command be available in DMs.
        sub_cmd_name: 1-32 character name of the subcommand
        sub_cmd_description: 1-100 character description of the subcommand
        group_name: 1-32 character name of the group
        group_description: 1-100 character description of the group
        nsfw: This command should only work in NSFW channels
    Returns:
        TansySlashCommand Object
    """

    def wrapper(func: ipy.const.AsyncCallable) -> TansySlashCommand:
        if not inspect.iscoroutinefunction(func):
            raise ValueError("Commands must be coroutines")

        perm = default_member_permissions
        if hasattr(func, "default_member_permissions"):
            if perm:
                perm = perm | func.default_member_permissions
            else:
                perm = func.default_member_permissions

        _description = description
        if _description is ipy.MISSING:
            _description = func.__doc__ or "No Description Set"

        return TansySlashCommand(
            name=name,
            group_name=group_name,
            group_description=group_description,
            sub_cmd_name=sub_cmd_name,
            sub_cmd_description=sub_cmd_description,
            description=_description,
            scopes=scopes or [ipy.const.GLOBAL_SCOPE],
            default_member_permissions=perm,
            dm_permission=dm_permission,
            nsfw=nsfw,
            callback=func,
        )

    return wrapper


def tansy_subcommand(
    base: str | ipy.LocalisedName,
    *,
    subcommand_group: typing.Optional[str | ipy.LocalisedName] = None,
    name: typing.Optional[str | ipy.LocalisedName] = None,
    description: ipy.Absent[str | ipy.LocalisedDesc] = ipy.MISSING,
    base_description: typing.Optional[str | ipy.LocalisedDesc] = None,
    base_desc: typing.Optional[str | ipy.LocalisedDesc] = None,
    base_default_member_permissions: typing.Optional["ipy.Permissions"] = None,
    base_dm_permission: bool = True,
    subcommand_group_description: typing.Optional[str | ipy.LocalisedDesc] = None,
    sub_group_desc: typing.Optional[str | ipy.LocalisedDesc] = None,
    scopes: typing.List["ipy.Snowflake_Type"] = None,
    nsfw: bool = False,
) -> typing.Callable[[ipy.const.AsyncCallable], TansySlashCommand]:
    """
    A decorator specifically tailored for creating Tansy subcommands.
    Args:
        base: The name of the base command
        subcommand_group: The name of the subcommand group, if any.
        name: The name of the subcommand, defaults to the name of the coroutine.
        description: The description of the subcommand
        base_description: The description of the base command
        base_desc: An alias of `base_description`
        base_default_member_permissions: What permissions members need to have by default to use this command.
        base_dm_permission: Should this command be available in DMs.
        subcommand_group_description: Description of the subcommand group
        sub_group_desc: An alias for `subcommand_group_description`
        scopes: The scopes of which this command is available, defaults to GLOBAL_SCOPE
        nsfw: This command should only work in NSFW channels
    Returns:
        A SlashCommand object
    """

    def wrapper(func: ipy.const.AsyncCallable) -> TansySlashCommand:
        if not asyncio.iscoroutinefunction(func):
            raise ValueError("Commands must be coroutines")

        _description = description
        if _description is ipy.MISSING:
            _description = func.__doc__ or "No Description Set"

        cmd = TansySlashCommand(
            name=base,
            description=(base_description or base_desc) or "No Description Set",
            group_name=subcommand_group,
            group_description=(subcommand_group_description or sub_group_desc)
            or "No Description Set",
            sub_cmd_name=name,
            sub_cmd_description=_description,
            default_member_permissions=base_default_member_permissions,
            dm_permission=base_dm_permission,
            scopes=scopes or [ipy.const.GLOBAL_SCOPE],
            callback=func,
            nsfw=nsfw,
        )
        return cmd

    return wrapper


SlashCommand = TansySlashCommand
slash_command = tansy_slash_command
subcommand = tansy_subcommand
