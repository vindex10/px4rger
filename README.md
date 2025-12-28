# PX4rger

Live update of px4 parameters.

## Features:

* supports importing a version from file or from a remote url
* if the version defined in the file's header didn't change since the last successful update - skip
* do not send update requests for parameters that didn't change. check first.
* re-apply parameters until no changes neccessary. some parameters lead to new parameters appearing in the spec.
* revert if not ready to fly after applying
