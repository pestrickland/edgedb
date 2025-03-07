.. _ref_guide_deployment_bare_metal:

==========
Bare Metal
==========

In this guide we show how to deploy EdgeDB to bare metal using your system's
package manager and systemd.


Install the EdgeDB Package
==========================

The steps for installing the EdgeDB package will be slightly different
depending on your Linux distribution. Once you have the package installed you
can jump to :ref:`ref_guide_deployment_bare_metal_enable_unit`.


Debian/Ubuntu LTS
-----------------
Import the EdgeDB packaging key.

.. code-block:: bash

   $ curl https://packages.edgedb.com/keys/edgedb.asc \
       | sudo apt-key add -

Add the EdgeDB package repository.

.. code-block:: bash

   $ echo deb https://packages.edgedb.com/apt $(lsb_release -cs) main \
       | sudo tee /etc/apt/sources.list.d/edgedb.list

Install the EdgeDB package.

.. code-block:: bash

   $ sudo apt-get update && sudo apt-get install edgedb-1


CentOS/RHEL 7/8
---------------
Add the EdgeDB package repository.

.. code-block:: bash

   $ sudo curl -fL https://packages.edgedb.com/rpm/edgedb-rhel.repo \
       > /etc/yum.repos.d/edgedb.repo

Install the EdgeDB package.

.. code-block:: bash

   $ sudo yum install edgedb-1


.. _ref_guide_deployment_bare_metal_enable_unit:

Enable a systemd unit
=====================

The EdgeDB package comes bundled with a systemd unit that is disabled by
default. You can start the server by enabling the unit.

.. code-block:: bash

   $ systemctl enable --now edgedb-server-1

This will start the server on port 5656, and the data directory will be
``/var/lib/edgedb/1/data``. You can edit the unit to specify server arguments
via the environment. The variables are largely the same as :ref:`those
documented for Docker <ref_guides_deployment_docker_customization>`.
