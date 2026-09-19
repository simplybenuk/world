# World

**What if applications didn't exist?**

Today's software usually combines three things:

1. A system of record
2. Application logic
3. A user interface

We build databases, put APIs and application code in front of them, then build interfaces that determine how people can interact with the underlying information.

World explores a different model:

> **The system of record persists. The application is temporary.**

Instead of building applications around data, World describes a persistent world of records, relationships, rules and capabilities.

Humans and agents can inspect that world and decide how they want to interact with it.

The interface can be created on demand, on the client, for the user's current intent.

## The idea

Imagine a simple project management system.

Its persistent state might contain:

```text
Person
Project
Task
Comment
```

A task might look something like:

```text
Task {
    title: "Write launch announcement"
    project: Project#website
    owner: Person#ben
    status: doing
    due: 2026-09-25
}
```

World knows that this record exists.

It knows its type.

It knows how it relates to other records.

It knows what values are valid.

It knows who can see it.

It knows who can change it.

It knows how it has changed.

It does **not** know what a Kanban board is.

## No application UI

World intentionally contains no definitions for:

- pages
- screens
- forms
- dashboards
- navigation
- tables
- Kanban boards
- mobile layouts

Those belong to the client.

A user might ask:

> Show me what needs my attention today.

Their client could inspect the world and generate an appropriate interface.

Another user might ask:

> Give me an overview of the website project.

The same records could produce a completely different interface.

The first user could then say:

> Show this as a Kanban board.

Nothing about the underlying system changes.

The interface is disposable.

The records are not.

## World as a protocol

Traditional applications expose operations anticipated by their developers.

```text
GET /projects
POST /tasks
PATCH /tasks/:id
GET /dashboard
```

World should instead expose the world itself.

Conceptually:

```text
describe world

describe Task

query Task where status != done

get Task#123

create Task {
    title: "Write documentation"
    project: Project#world
}

change Task#123 {
    status: done
}

history Task#123
```

A client should be able to discover:

- what kinds of records exist
- what fields they contain
- how records relate
- what constraints exist
- what the current actor can see
- what the current actor can change
- what has previously happened

The client can then decide what interaction makes sense.

## Records

The core primitive in World is the record.

A possible future definition might look like:

```text
record Task {
    title: Text
    project: Project
    owner: Person?
    status: todo | doing | done
    due: Date?
}
```

Records can reference other records:

```text
record Project {
    name: Text
    members: [Person]
}
```

Exactly what this language looks like is deliberately undecided.

Part of this experiment is discovering whether a new language is useful at all.

## Rules and capabilities

Describing data isn't enough.

Agents need to understand the boundaries of the world they are operating within.

A World therefore needs to describe what changes are permitted.

For example:

```text
record Task {
    title: Text
    status: todo | doing | done
    owner: Person?

    allow create when actor.memberOf(project)
    allow update when actor.memberOf(project)
    allow delete when actor.role == admin
}
```

A client should not need application-specific knowledge to understand that a task can be created or that its status can be changed.

More importantly, an agent cannot simply bypass those rules.

The rules belong to the world, not the interface.

## Intent

CRUD may itself be too application-centric.

Instead of telling World exactly how to manipulate records, a client could eventually express a desired state:

```text
ensure Task {
    project: Project#world
    title: "Publish documentation"
    status: todo
}
```

World could determine whether that requires creating something, changing something or doing nothing.

This is an area for experimentation rather than an established design.

## History and provenance

Agent-driven systems make provenance important.

A change should be able to answer:

```text
what changed?
who changed it?
why?
on whose behalf?
using what evidence?
```

For example:

```text
Task#123.status = done

because {
    actor: Agent#assistant
    requestedBy: Person#ben
    reason: "Documentation merged"
    evidence: Commit#abc123
}
```

History should be part of the model rather than an application logging feature added later.

## The hypothesis

World is based on a few assumptions worth testing.

### 1. Interfaces are becoming cheap

Generative systems can increasingly create interfaces at runtime.

If an interface can be produced cheaply for a specific person and task, permanently designing every possible interface becomes less important.

### 2. Data is durable

Interfaces change.

Frameworks change.

Applications get replaced.

The underlying records often survive all of them.

### 3. Agents need worlds, not screens

Most software is currently designed for humans to operate through graphical interfaces.

Agents are then taught to use those interfaces or given application-specific APIs.

World asks whether agents could instead operate directly against a governed model of the underlying domain.

### 4. Humans and agents can share the same model

Humans might interact through generated interfaces.

Agents might interact directly.

Other software might interact through a protocol.

All of them operate on the same records and under the same rules.

## First experiment

The first World should be deliberately tiny.

Four record types:

```text
Person
Project
Task
Comment
```

Seed it with enough records to represent a small working team.

Then build a generic client capable of discovering the world.

The target demonstration:

```text
open client

> Show me what needs attention.

[interface generated]

> Make this more visual.

[interface changes]

> Show the website project instead.

[different interface generated]

> Mark the documentation task as complete.

[record changes]
```

Close the client.

Open another client.

The interfaces are gone.

The change remains.

## What World is not

World is not currently intended to be:

- a frontend framework
- a CMS
- a database
- a low-code platform
- a project management application
- an AI website builder
- another REST or GraphQL framework

It is an experiment in separating **persistent systems of record** from **temporary applications**.

Some existing technologies already explore parts of this space.

World is an attempt to see what happens when that separation becomes the starting assumption.

## Design constraint

One rule should remain true while exploring the idea:

> **World does not contain UI definitions.**

If World needs to know what a button, form, dashboard or Kanban board is, the abstraction has probably leaked.

## Status

Very experimental.

There is no settled architecture, protocol or language yet.

The first goal is not to build a production framework.

It is to answer a question:

> **If interfaces become disposable, what should the software underneath them look like?**
