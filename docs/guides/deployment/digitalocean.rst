.. _ref_guide_deployment_digitalocean:

===============
On DigitalOcean
===============

In this guide we show how to deploy EdgeDB to DigitalOcean either with a
One-click Deploy option or a
:ref:`managed PostgreSQL <ref_guide_deployment_digitalocean_managed>`
database as the backend.


One-click Deploy
++++++++++++++++

Prerequisites
=============

* ``edgedb`` CLI (`install <edgedb-install_>`_)
* DigitalOcean account

Click the button below and follow the droplet creation workflow on
DigitalOcean. Once deployed you will have an EdgeDB instance running. The
default admin password is ``edgedbpassword``. We strongly recommend that you
change the password.

.. image:: images/do-btn-blue.svg
   :target: 1-click-button_

.. _1-click-button:
   https://marketplace.digitalocean.com/apps/edgedb?refcode=f0b0d77b5d49

To change the password run the following. You will find your droplet ip address
on digitalocean_.

.. _digitalocean: https://cloud.digitalocean.com/droplets?
.. _here: edgedb-install_

.. code-block:: bash

   $ IP=<your-droplet-ip>
   $ read -sp "Password: " PASSWORD
   $ printf edgedbpassword | edgedb query \
         --host $IP \
         --password-from-stdin \
         --tls-security insecure \
         "alter role edgedb set password := '${PASSWORD}'"
   OK: ALTER ROLE

You can now link and connect to the new EdgeDB instance:

.. code-block:: bash

   $ printf $PASSWORD | edgedb instance link \
         --password-from-stdin \
         --trust-tls-cert \
         --host $IP \
         --non-interactive \
         digitalocean
   Authenticating to edgedb://edgedb@your-droplet-ip:5656/edgedb
   Trusting unknown server certificate
   Successfully linked to remote instance. To connect run:
     edgedb -I digitalocean

You can now use the EdgeDB instance deployed on DigitalOcean as
``digitalocean``, for example:

.. code-block:: bash

   $ edgedb -I digitalocean
   edgedb>


.. _ref_guide_deployment_digitalocean_managed:

Deploy with Managed PostgreSQL
++++++++++++++++++++++++++++++

Prerequisites
=============

* ``edgedb`` CLI (`install <edgedb-install_>`_)
* DigitalOcean account
* ``doctl`` CLI (`install <doclt-install_>`_)
* ``jq`` (`install <jq_>`_)

.. _edgedb-install: https://www.edgedb.com/install
.. _doclt-install: https://docs.digitalocean.com/reference/doctl/how-to/install
.. _jq: https://stedolan.github.io/jq/


Create a managed PostgreSQL instance
====================================

If you already have a PostgreSQL instance you can skip this step.

.. code-block:: bash

   $ DSN="$( \
         doctl databases create edgedb-postgres \
             --engine pg \
             --version 13 \
             --size db-s-1vcpu-1gb \
             --num-nodes 1 \
             --region sfo3 \
             --output json \
         | jq -r '.[0].connection.uri' )"


Provision a droplet
===================

Replace ``$SSH_KEY_IDS`` with the ids for the ssh keys you want to ssh into the
new droplet with. Separate multiple values with a comma. You can list your
keys with ``doctl compute ssh-key list``.  If you don't have any ssh keys in
your DigitalOcean account you can follow `this guide <upload-ssh-keys_>`_ to
add one now.

.. _upload-ssh-keys:
   https://docs.digitalocean.com/products/droplets
   /how-to/add-ssh-keys/to-account/

.. code-block:: bash

   $ IP="$( \
         doctl compute droplet create edgedb \
             --image edgedb \
             --region sfo3 \
             --size s-2vcpu-4gb \
             --ssh-keys $SSH_KEY_IDS \
             --format PublicIPv4 \
             --no-header \
             --wait )"

Configure the backend postgres DSN. To simplify the initial deployment, let's
instruct EdgeDB to run in insecure mode (with password authentication off and
an autogenerated TLS certificate). We will secure the instance once things are
up and running.

.. code-block:: bash

   $ printf "EDGEDB_SERVER_BACKEND_DSN=${DSN} \
   \nEDGEDB_SERVER_SECURITY=insecure_dev_mode\n" \
   | ssh root@$IP -T "cat > /etc/edgedb/env"

   $ ssh root@$IP "systemctl restart edgedb.service"

Set the superuser password.

.. code-block:: bash

   $ read -srp "Admin password: " PASSWORD

   $ edgedb -H $IP --tls-security insecure query \
         "alter role edgedb set password := '$PASSWORD'"
   OK: ALTER ROLE

Set the security policy to strict.

.. code-block:: bash

   $ printf "EDGEDB_SERVER_BACKEND_DSN=${DSN} \
   \nEDGEDB_SERVER_SECURITY=strict\n" \
   | ssh root@$IP -T "cat > /etc/edgedb/env"

   $ ssh root@$IP "systemctl restart edgedb.service"

That's it! You can now start using the EdgeDB instance located at
``edgedb://$IP``.


.. _ref_guide_deployment_digitalocean_link:

Create a local link to the new EdgeDB instance
==============================================

To access the EdgeDB instance you've just provisioned on DigitalOcean from your
local machine run the following command.

.. code-block:: bash

   $ printf $PASSWORD | edgedb instance link \
         --password-from-stdin \
         --trust-tls-cert \
         --host $IP \
         --non-interactive \
         digitalocean
   Authenticating to edgedb://edgedb@137.184.227.94:5656/edgedb
   Trusting unknown server certificate:
   SHA1:1880da9527be464e2cad3bdb20dfc430a6af5727
   Successfully linked to remote instance. To connect run:
     edgedb -I digitalocean

You can now use the EdgeDB instance deployed on DigitalOcean as
``digitalocean``, for example:

.. code-block:: bash

   $ edgedb -I digitalocean
   edgedb>
